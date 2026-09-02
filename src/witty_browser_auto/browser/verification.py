"""浏览器动作的确定性后置条件校验。"""

from __future__ import annotations

import asyncio
import json
from typing import Protocol

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.domain.errors import CdpCommandError, TargetNotFoundError
from witty_browser_auto.domain.models import (
    CandidateTarget,
    DragRiskClass,
    ExpectedCondition,
    Observation,
    VerificationResult,
)

_CHALLENGE_FAILURE_TERMS = ("验证失败", "验证未通过", "请刷新", "重新验证")
_OBSERVATION_POLL_SECONDS = 0.05


class VerificationDriver(Protocol):
    _candidate_cache: dict[str, CandidateTarget]

    async def observe(self, *, force: bool = False) -> Observation: ...

    def _matches_locator_recipe(
        self,
        previous: CandidateTarget,
        current: CandidateTarget,
    ) -> bool: ...

    def _require_session(self) -> CdpTargetSession: ...

    async def _verification_document_text(self) -> str: ...


async def verify_condition(
    driver: VerificationDriver,
    condition: ExpectedCondition,
) -> VerificationResult:
    if condition.kind == "target_exists":
        return await _verify_target_exists(driver, condition.value)
    if condition.kind == "fingerprint_changed":
        return await _verify_fingerprint_changed(driver, condition)
    if condition.kind == "challenge_refreshed":
        return await _verify_challenge_refreshed(driver, condition)
    if condition.kind == "challenge_ready":
        return await _verify_challenge_ready(driver, condition)
    if condition.kind == "challenge_cleared":
        return await _verify_challenge_cleared(driver, condition)
    if condition.kind == "page_visible":
        return await _verify_page_visible(driver, condition)
    return await _verify_positive_condition(driver, condition)


async def _verify_fingerprint_changed(
    driver: VerificationDriver,
    condition: ExpectedCondition,
) -> VerificationResult:
    """SPA 和验证码通常晚于点击回执更新，必须在声明窗口内持续观察。"""

    deadline = asyncio.get_running_loop().time() + condition.timeout_seconds
    while True:
        observation = await driver.observe(force=True)
        if observation.fingerprint != condition.value:
            return VerificationResult(True, "页面状态已变化")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return VerificationResult(False, "页面状态尚未变化")
        await asyncio.sleep(min(_OBSERVATION_POLL_SECONDS, remaining))


async def _verify_challenge_ready(
    driver: VerificationDriver,
    condition: ExpectedCondition,
) -> VerificationResult:
    deadline = asyncio.get_running_loop().time() + condition.timeout_seconds
    while True:
        observation = await driver.observe(force=True)
        ready = _challenge_drag_ready(observation)
        if ready:
            return VerificationResult(True, "安全挑战控件已就绪")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return VerificationResult(False, "安全挑战控件仍在渲染")
        await asyncio.sleep(min(_OBSERVATION_POLL_SECONDS, remaining))


async def _verify_challenge_refreshed(
    driver: VerificationDriver,
    condition: ExpectedCondition,
) -> VerificationResult:
    """刷新必须离开旧失败态并重新出现可拖拽控件，像素变化本身不算成功。"""

    deadline = asyncio.get_running_loop().time() + condition.timeout_seconds
    while True:
        observation = await driver.observe(force=True)
        if observation.fingerprint != condition.value and _challenge_drag_ready(observation):
            return VerificationResult(True, "安全挑战已刷新并重新就绪")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return VerificationResult(False, "安全挑战刷新后未出现新的可拖拽控件")
        await asyncio.sleep(min(_OBSERVATION_POLL_SECONDS, remaining))


def _challenge_drag_ready(observation: Observation) -> bool:
    summary = f"{observation.title}\n{observation.summary}"
    if any(term in summary for term in _CHALLENGE_FAILURE_TERMS):
        return False
    return observation.visual_drag_risk is DragRiskClass.SECURITY and any(
        candidate.drag_risk is DragRiskClass.SECURITY
        or (
            candidate.box is not None
            and candidate.box.height >= 8
            and candidate.box.width >= candidate.box.height * 4
        )
        for candidate in observation.candidates
    )


async def _verify_target_exists(
    driver: VerificationDriver,
    target_id: str,
) -> VerificationResult:
    previous = driver._candidate_cache.get(target_id)
    if previous is None:
        return VerificationResult(False, "目标区域已经失效，缺少旧观察上下文")
    observation = await driver.observe(force=True)
    matches = [
        item for item in observation.candidates if driver._matches_locator_recipe(previous, item)
    ]
    if len(matches) == 1:
        return VerificationResult(True, "目标区域存在")
    if len(matches) > 1:
        return VerificationResult(False, "目标区域重新定位后出现歧义")
    return VerificationResult(False, "目标区域不存在")


async def _verify_challenge_cleared(
    driver: VerificationDriver,
    condition: ExpectedCondition,
) -> VerificationResult:
    session = driver._require_session()
    expression = "(() => ({text: document.body?.innerText || '', ready: document.readyState}))()"
    deadline = asyncio.get_running_loop().time() + condition.timeout_seconds
    consecutive_clear_checks = 0
    while asyncio.get_running_loop().time() < deadline:
        try:
            result = await session.call(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
        except CdpCommandError:
            await asyncio.sleep(0.05)
            continue
        value = result.get("result", {}).get("value", {})
        text = str(value.get("text", "")) if isinstance(value, dict) else ""
        if any(term in text for term in _CHALLENGE_FAILURE_TERMS):
            return VerificationResult(False, "安全挑战已明确返回失败")
        ready = value.get("ready") if isinstance(value, dict) else None
        if text.strip() and condition.value not in text and ready != "loading":
            consecutive_clear_checks += 1
            if consecutive_clear_checks >= 2:
                return VerificationResult(True, "安全挑战页面已消失")
        else:
            consecutive_clear_checks = 0
        await asyncio.sleep(0.05)
    return VerificationResult(False, "等待安全挑战完成超时")


async def _verify_page_visible(
    driver: VerificationDriver,
    condition: ExpectedCondition,
) -> VerificationResult:
    """锁屏、最小化或后台舞台期间不发送验证码指针事件。"""

    session = driver._require_session()
    deadline = asyncio.get_running_loop().time() + condition.timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            result = await session.call(
                "Runtime.evaluate",
                {
                    "expression": "document.visibilityState",
                    "returnByValue": True,
                },
            )
        except CdpCommandError:
            await asyncio.sleep(0.05)
            continue
        if result.get("result", {}).get("value") == "visible":
            return VerificationResult(True, "浏览器页面已恢复可见")
        await asyncio.sleep(0.05)
    return VerificationResult(False, "浏览器页面仍不可见")


async def _verify_positive_condition(
    driver: VerificationDriver,
    condition: ExpectedCondition,
) -> VerificationResult:
    if condition.kind == "text_contains":
        return await _verify_text_contains(driver, condition)
    session = driver._require_session()
    if condition.kind == "url_contains":
        expression = f"location.href.includes({json.dumps(condition.value)})"
    elif condition.kind == "title_contains":
        expression = f"document.title.includes({json.dumps(condition.value)})"
    else:
        return VerificationResult(False, f"不支持的校验条件：{condition.kind}")

    deadline = asyncio.get_running_loop().time() + condition.timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            result = await session.call(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
        except CdpCommandError:
            await asyncio.sleep(0.1)
            continue
        if result.get("result", {}).get("value") is True:
            return VerificationResult(True, "业务后置条件已满足")
        await asyncio.sleep(0.1)
    return VerificationResult(False, "等待业务后置条件超时")


async def _verify_text_contains(
    driver: VerificationDriver,
    condition: ExpectedCondition,
) -> VerificationResult:
    """文本校验跟随动作所在的帧。

    iframe 里的文字不会出现在主文档的 innerText 中，如果始终只读主框架，任何在 iframe
    内完成的操作都无法通过后置校验。
    """

    deadline = asyncio.get_running_loop().time() + condition.timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            text = await driver._verification_document_text()
        except (CdpCommandError, TargetNotFoundError):
            await asyncio.sleep(0.1)
            continue
        if condition.value in text:
            return VerificationResult(True, "业务后置条件已满足")
        await asyncio.sleep(0.1)
    return VerificationResult(False, "等待业务后置条件超时")

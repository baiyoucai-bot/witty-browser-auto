"""采集程序的存储前验证门：重进入口、结构探针通过后才晋升为可重放程序。

设计约束 (VERIFIED_PROGRAM_REPLAY_DESIGN P0)：
- 首跑成功只产生候选；必须在干净入口状态下重新执行结构探针，证明规格
  不依赖当次会话的临时 DOM 状态，才允许写入程序库。
- 门与晋升都是固定代码，模型不参与；任何失败只记录事件，不影响任务终态。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from witty_browser_auto.agent.navigation_policy import assert_navigation_allowed
from witty_browser_auto.domain.errors import PolicyViolationError
from witty_browser_auto.domain.extraction import (
    CollectionExtractionResult,
    CollectionExtractionSpec,
    collection_structure_fingerprint,
    evaluate_entry_probe,
)
from witty_browser_auto.domain.models import ActionCommand, ActionKind, TaskSpec
from witty_browser_auto.domain.protocols import AutomationDriver, StructuredDataExtractor
from witty_browser_auto.memory.background import BackgroundMemoryRuntime
from witty_browser_auto.security.redaction import sanitize_url_for_storage

logger = logging.getLogger(__name__)

_PROBE_RETRY_INTERVAL_SECONDS = 0.3


def scenario_key(goal: str) -> str:
    """把任务目标归一化后哈希为稳定场景键，用于跨任务命中同一采集程序。"""

    normalized = " ".join(goal.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class CollectionProgramHost(Protocol):
    driver: AutomationDriver
    memory_runtime: BackgroundMemoryRuntime | None
    structured_extractor: StructuredDataExtractor | None

    async def emit_program_event(
        self,
        kind: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None: ...


def spec_contains_task_inputs(spec: Mapping[str, Any], inputs: Mapping[str, Any]) -> bool:
    """阻止任务输入值被固化进可跨任务复用的采集程序。"""

    values = {str(value) for value in inputs.values() if str(value)}
    if not values:
        return False
    spec_text = json.dumps(spec, ensure_ascii=False)
    return any(value in spec_text for value in values)


async def probe_entry_until_ready(
    extractor: StructuredDataExtractor,
    spec: CollectionExtractionSpec,
    *,
    timeout_seconds: float,
) -> str | None:
    """在超时预算内反复执行入口结构探针，返回最后一次拒绝原因，None 表示通过。

    列表页在导航后往往异步渲染，单次探针会把加载中间态误判为结构失配。
    """

    deadline = asyncio.get_running_loop().time() + max(0.0, timeout_seconds)
    reason: str | None = "入口结构探针未执行"
    while True:
        try:
            probe = await extractor.probe_entry(spec)
        except (RuntimeError, ValueError) as exc:
            reason = f"入口结构探针执行失败：{exc}"
        else:
            reason = evaluate_entry_probe(spec, probe)
            if reason is None:
                return None
        if asyncio.get_running_loop().time() >= deadline:
            return reason
        await asyncio.sleep(_PROBE_RETRY_INTERVAL_SECONDS)


async def verify_and_promote_collection_program(
    host: CollectionProgramHost,
    task: TaskSpec,
    spec: CollectionExtractionSpec,
    entry_url: str,
    result: CollectionExtractionResult,
    evidence_id: str,
) -> bool:
    """对本次成功采集执行存储前验证门；通过后异步晋升为可重放程序。

    返回是否已提交晋升。任何拒绝或异常只产生事件与日志，不影响任务终态。
    """

    try:
        return await _verify_and_promote(host, task, spec, entry_url, result, evidence_id)
    except Exception as exc:
        # 晋升是任务成功后的增值动作，任何未预期异常都不允许改变已完成的终态。
        logger.warning(
            "采集程序验证门执行异常，跳过晋升",
            extra={"task_id": task.task_id, "error_type": type(exc).__name__},
        )
        return False


async def _verify_and_promote(
    host: CollectionProgramHost,
    task: TaskSpec,
    spec: CollectionExtractionSpec,
    entry_url: str,
    result: CollectionExtractionResult,
    evidence_id: str,
) -> bool:
    if host.structured_extractor is None or host.memory_runtime is None:
        return False
    if not (result.complete and result.has_strong_completion_evidence):
        return False
    if spec_contains_task_inputs(spec.to_mapping(), task.inputs):
        await _emit_rejected(host, task, "采集规格包含任务输入值，拒绝固化为跨任务程序")
        return False
    try:
        assert_navigation_allowed(task, entry_url)
    except PolicyViolationError as exc:
        await _emit_rejected(host, task, f"入口地址不满足导航策略：{exc}")
        return False

    # 验证门第一步：离开当次终态页面，从入口 URL 重新进入，拿到干净状态。
    receipt = await host.driver.execute(
        ActionCommand(
            action_id=f"program-gate-{uuid.uuid4().hex}",
            kind=ActionKind.NAVIGATE,
            url=entry_url,
            idempotent=True,
        )
    )
    if not receipt.success:
        await _emit_rejected(host, task, f"验证门重进入口失败：{receipt.message}")
        return False

    reason = await probe_entry_until_ready(
        host.structured_extractor,
        spec,
        timeout_seconds=spec.page_wait_timeout_seconds,
    )
    if reason is not None:
        await _emit_rejected(host, task, f"入口结构探针未通过：{reason}")
        return False

    fingerprint = collection_structure_fingerprint(spec)
    summary = {
        "unique_count": result.unique_count,
        "exported_count": result.exported_count,
        "visited_page_count": len(result.visited_pages),
        "pagination_mode": result.pagination_mode,
        "completion_evidence": list(result.completion_evidence),
        "detail_requested": result.detail_requested,
        "detail_count": result.detail_count,
    }
    host.memory_runtime.save_collection_program_later(
        scope=task.scope,
        scenario_key=scenario_key(task.goal),
        url=sanitize_url_for_storage(entry_url, task.inputs),
        structure_fingerprint=fingerprint,
        spec=spec.to_mapping(),
        summary=summary,
        evidence_id=evidence_id,
        metadata={"task_id": task.task_id, "gate": "entry_probe_v1"},
    )
    await host.emit_program_event(
        "collection_program_promoted",
        "采集程序已通过存储前验证门，后续同场景任务可零模型重放",
        {
            "structure_fingerprint": fingerprint[:16],
            "pagination_mode": spec.pagination_mode,
            "field_count": len(spec.fields),
            "unique_count": result.unique_count,
        },
    )
    return True


async def _emit_rejected(host: CollectionProgramHost, task: TaskSpec, reason: str) -> None:
    logger.info("采集程序未通过验证门", extra={"task_id": task.task_id, "reason": reason})
    await host.emit_program_event("collection_program_rejected", reason, {"reason": reason})

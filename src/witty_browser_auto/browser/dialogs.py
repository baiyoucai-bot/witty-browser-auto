"""JavaScript 原生对话框的接管。

`Page.enable` 之后，`alert`/`confirm`/`prompt`/`beforeunload` 会把渲染进程挂起，直到
调试端显式应答。没有应答就意味着后续 `Runtime.evaluate`、`DOM.getDocument`、截图
全部超时——会话实际已经报废。所以接管不是可选能力，而是启用 Page 域的必然义务：
只要订阅了事件就必须回答，策略只决定回答什么。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from witty_browser_auto.domain.models import utc_now

logger = logging.getLogger(__name__)

DIALOG_KINDS: tuple[str, ...] = ("alert", "confirm", "prompt", "beforeunload")
DIALOG_ACTIONS: tuple[str, ...] = ("accept", "dismiss")

_MAX_DIALOG_LOG = 50
_MAX_MESSAGE_LENGTH = 500
_MAX_PROMPT_TEXT = 1000

# 默认策略按"不替用户做不可逆决定"选取：
# alert 只有一个按钮，accept 与 dismiss 等价；confirm 与 prompt 一律取消，
# 因为它们背后通常是删除、覆盖、提交这类不可逆操作，替调用方点确定是越权；
# beforeunload 则相反——导航是调用方刚刚显式要求的，拦下自己的导航才是意外。
_DEFAULT_POLICY: dict[str, str] = {
    "alert": "accept",
    "confirm": "dismiss",
    "prompt": "dismiss",
    "beforeunload": "accept",
}


class DialogResponder(Protocol):
    """能够应答对话框的会话。"""

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DialogRecord:
    """一次已应答的对话框。"""

    kind: str
    message: str
    url: str
    action: str
    prompt_text: str
    default_prompt: str
    handled_by: str
    occurred_at: datetime

    def public_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "url": self.url,
            "action": self.action,
            "prompt_text": self.prompt_text,
            "default_prompt": self.default_prompt,
            "handled_by": self.handled_by,
            "occurred_at": self.occurred_at.isoformat(),
        }

    def model_dict(self) -> dict[str, Any]:
        # 对话框文案本身是页面给的提示，可以给模型；填进 prompt 的值可能来自任务输入，不给。
        payload = self.public_dict()
        payload["prompt_text"] = "[REDACTED]" if self.prompt_text else ""
        return payload


@dataclass(slots=True)
class DialogRule:
    """对某一类对话框的应答约定。"""

    action: str
    prompt_text: str = ""
    once: bool = False


@dataclass(slots=True)
class DialogSupervisor:
    """按策略应答对话框，并留下调用方可查的记录。"""

    _overrides: dict[str, DialogRule] = field(default_factory=dict)
    _records: deque[DialogRecord] = field(default_factory=lambda: deque(maxlen=_MAX_DIALOG_LOG))
    _seen: int = 0

    # ---- 策略 ----

    def set_rule(
        self,
        action: str,
        *,
        prompt_text: str = "",
        once: bool,
        kinds: tuple[str, ...] = DIALOG_KINDS,
    ) -> None:
        if action not in DIALOG_ACTIONS:
            raise ValueError(f"对话框应答只能是 accept 或 dismiss：{action}")
        if len(prompt_text) > _MAX_PROMPT_TEXT:
            raise ValueError(f"prompt 文本不能超过 {_MAX_PROMPT_TEXT} 个字符")
        for kind in kinds:
            if kind not in DIALOG_KINDS:
                raise ValueError(f"未知的对话框类型：{kind}")
            self._overrides[kind] = DialogRule(action, prompt_text, once)

    def clear_rules(self) -> None:
        self._overrides.clear()

    def effective_policy(self) -> dict[str, dict[str, Any]]:
        policy: dict[str, dict[str, Any]] = {}
        for kind in DIALOG_KINDS:
            rule = self._overrides.get(kind)
            policy[kind] = {
                "action": rule.action if rule else _DEFAULT_POLICY[kind],
                "source": ("一次性覆盖" if rule.once else "会话覆盖") if rule else "默认",
            }
        return policy

    # ---- 记录 ----

    @property
    def seen(self) -> int:
        return self._seen

    def records(self) -> tuple[DialogRecord, ...]:
        return tuple(self._records)

    # ---- 应答 ----

    def decide(self, kind: str) -> tuple[str, str, str]:
        """返回 (动作, prompt 文本, 决策来源)；一次性规则在此消费。"""

        rule = self._overrides.get(kind)
        if rule is None:
            return _DEFAULT_POLICY.get(kind, "dismiss"), "", "默认策略"
        if rule.once:
            del self._overrides[kind]
            return rule.action, rule.prompt_text, "一次性覆盖"
        return rule.action, rule.prompt_text, "会话覆盖"

    async def handle_event(self, session: DialogResponder, params: dict[str, Any]) -> None:
        kind = str(params.get("type", "")) or "alert"
        message = str(params.get("message", ""))[:_MAX_MESSAGE_LENGTH]
        url = str(params.get("url", ""))
        default_prompt = str(params.get("defaultPrompt", ""))[:_MAX_MESSAGE_LENGTH]
        action, prompt_text, source = self.decide(kind)

        payload: dict[str, Any] = {"accept": action == "accept"}
        if kind == "prompt" and action == "accept":
            payload["promptText"] = prompt_text or default_prompt
        try:
            # 渲染进程此刻是挂起的，但对话框由浏览器进程持有，这条命令仍然可达。
            await session.call("Page.handleJavaScriptDialog", payload, timeout_seconds=5)
        except Exception as exc:
            logger.warning(
                "应答 JavaScript 对话框失败，页面可能仍处于挂起状态",
                extra={"dialog_kind": kind, "exception_type": type(exc).__name__},
            )
            return

        self._seen += 1
        self._records.append(
            DialogRecord(
                kind=kind,
                message=message,
                url=url,
                action=action,
                prompt_text=str(payload.get("promptText", "")),
                default_prompt=default_prompt,
                handled_by=source,
                occurred_at=utc_now(),
            )
        )
        logger.info(
            "已应答 JavaScript 对话框",
            extra={"dialog_kind": kind, "dialog_action": action, "decided_by": source},
        )

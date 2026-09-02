"""对话框应答策略的执行层。

对话框必须在事件到达的那一刻就回答，等模型或调用方决定就已经太晚——页面在等待期间
是挂起的。所以这个工具设置的是"下一次/后续怎么答"，而不是"现在回答这一个"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from witty_browser_auto.browser.dialogs import DIALOG_ACTIONS, DIALOG_KINDS
from witty_browser_auto.domain.models import TaskSpec
from witty_browser_auto.domain.protocols import AutomationDriver, DialogControlProvider

DIALOG_TOOL_NAMES = frozenset({"handle_dialog"})

_ACTIONS: tuple[str, ...] = (*DIALOG_ACTIONS, "inspect")
_SCOPES: tuple[str, ...] = ("next", "session")
_MAX_RECORDS = 20


@dataclass(frozen=True, slots=True)
class DialogToolOutcome:
    message: str
    data: dict[str, Any]
    model_data: dict[str, Any]


def dialogs_available(driver: AutomationDriver) -> bool:
    capabilities = getattr(driver, "capabilities", None)
    return bool(getattr(capabilities, "dialogs", False)) and isinstance(
        driver, DialogControlProvider
    )


def execute_dialog_tool(
    arguments: Mapping[str, Any],
    *,
    driver: AutomationDriver,
    task: TaskSpec,
) -> DialogToolOutcome:
    if not dialogs_available(driver):
        raise ValueError("当前驱动不支持对话框接管")
    unknown = set(arguments) - {
        "action",
        "prompt_text",
        "prompt_text_input_key",
        "scope",
        "dialog_kinds",
    }
    if unknown:
        raise ValueError(f"handle_dialog 包含未知参数：{', '.join(sorted(unknown))}")

    action = arguments.get("action")
    if action not in _ACTIONS:
        raise ValueError(f"action 只能是 {'、'.join(_ACTIONS)}：{action}")

    provider: DialogControlProvider = driver  # type: ignore[assignment]

    if action != "inspect":
        scope = arguments.get("scope", "next")
        if scope not in _SCOPES:
            raise ValueError(f"scope 只能是 next 或 session：{scope}")
        kinds = _resolve_kinds(arguments.get("dialog_kinds"))
        prompt_text = _resolve_prompt_text(arguments, task)
        if prompt_text and action != "accept":
            raise ValueError("prompt 文本只在 action 为 accept 时有意义")
        provider.set_dialog_rule(
            action,
            prompt_text=prompt_text,
            once=scope == "next",
            kinds=kinds,
        )

    policy = provider.dialog_policy()
    records = provider.dialog_records()[-_MAX_RECORDS:]
    data = {
        "policy": policy,
        "dialogs": records,
        "dialog_count": len(provider.dialog_records()),
    }
    model_data = {
        "policy": policy,
        "dialogs": [_model_record(record) for record in records],
        "dialog_count": data["dialog_count"],
    }
    if action == "inspect":
        message = f"当前对话框策略已返回；本次会话共接管 {data['dialog_count']} 次对话框"
    else:
        target = "下一次" if arguments.get("scope", "next") == "next" else "后续所有"
        message = f"{target}对话框将以 {action} 应答"
    return DialogToolOutcome(message=message, data=data, model_data=model_data)


def _resolve_kinds(raw: Any) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ValueError("dialog_kinds 必须是非空数组")
    kinds: list[str] = []
    for item in raw:
        if item not in DIALOG_KINDS:
            raise ValueError(f"未知的对话框类型：{item}")
        if item not in kinds:
            kinds.append(str(item))
    return tuple(kinds)


def _resolve_prompt_text(arguments: Mapping[str, Any], task: TaskSpec) -> str:
    literal = arguments.get("prompt_text")
    input_key = arguments.get("prompt_text_input_key")
    if literal is not None and input_key is not None:
        raise ValueError("prompt_text 与 prompt_text_input_key 只能给一个")
    if input_key is not None:
        if not isinstance(input_key, str) or input_key not in task.inputs:
            raise ValueError(f"任务输入中不存在键 {input_key}")
        value = task.inputs[input_key]
        if not isinstance(value, str):
            raise ValueError(f"任务输入 {input_key} 不是字符串")
        return value
    if literal is None:
        return ""
    if not isinstance(literal, str):
        raise ValueError("prompt_text 必须是字符串")
    return literal


def _model_record(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    # 填进 prompt 的值可能来自任务输入，只告诉模型填没填。
    if payload.get("prompt_text"):
        payload["prompt_text"] = "[REDACTED]"
    return payload


def dialog_kind_names() -> Sequence[str]:
    return DIALOG_KINDS

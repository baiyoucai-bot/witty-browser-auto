"""显式定位器工具 schema 和确定性动作构造。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from witty_browser_auto.browser.form_fill import MAX_TEXT_LENGTH
from witty_browser_auto.browser.mouse import resolve_pointer
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionKind,
    ExpectedCondition,
    LocatorRecipe,
    ModelToolCall,
    TaskSpec,
)
from witty_browser_auto.toolkit.catalog import LOCATOR_TOOLS, names_of, schemas_of

LOCATOR_ACTION_TOOL_NAMES = names_of(LOCATOR_TOOLS)

LOCATOR_ACTION_SCHEMAS: tuple[dict[str, Any], ...] = schemas_of(LOCATOR_TOOLS)


def build_locator_command(
    call: ModelToolCall,
    task: TaskSpec,
    action_id: str,
    expected: ExpectedCondition | None,
) -> tuple[ActionCommand, str | None]:
    locator = locator_recipe(call.arguments)
    if call.name == "click_locator":
        if expected is None:
            raise ValueError("显式定位点击必须提供业务后置条件")
        if expected.kind == "target_exists":
            raise ValueError(
                "显式定位点击不能用 target_exists 作为后置条件，请使用页面状态或指纹条件"
            )
        button, click_count = resolve_pointer(
            call.arguments.get("button"), call.arguments.get("click_count")
        )
        return (
            ActionCommand(
                action_id,
                ActionKind.CLICK,
                locator=locator,
                expected=expected,
                idempotent=False,
                pointer_button=button,
                click_count=click_count,
            ),
            None,
        )
    if call.name == "input_text_locator":
        value, input_key = resolve_text_input(call.arguments, task)
        return (
            ActionCommand(
                action_id,
                ActionKind.INPUT_TEXT,
                locator=locator,
                value=value,
                idempotent=True,
            ),
            input_key,
        )
    if call.name == "select_locator":
        if expected is None:
            raise ValueError("显式定位选择必须提供业务后置条件")
        if expected.kind == "target_exists":
            raise ValueError(
                "显式定位选择不能用 target_exists 作为后置条件，请使用页面状态或指纹条件"
            )
        raw_input_key = call.arguments.get("input_key")
        if raw_input_key is not None:
            input_key = _required_text(call.arguments, "input_key", 100)
            if input_key not in task.inputs:
                raise ValueError(f"任务输入键不存在：{input_key}")
            value = str(task.inputs[input_key])
        else:
            input_key = None
            value = _required_text(call.arguments, "value", 1000)
        return (
            ActionCommand(
                action_id,
                ActionKind.SELECT,
                locator=locator,
                value=value,
                expected=expected,
                idempotent=True,
            ),
            input_key,
        )
    raise ValueError(f"不支持的显式定位工具：{call.name}")


def resolve_text_input(arguments: Mapping[str, Any], task: TaskSpec) -> tuple[str, str | None]:
    """解析文本输入的来源：`input_key` 引用任务输入，`text` 是非敏感字面量，二选一。

    返回 `(要输入的值, 输入键或 None)`。字面量若与任何任务输入的值相同会被拒绝——
    那是模型把见过的凭据抄进了参数，会让脱敏在轨迹里失守。
    """

    has_key = arguments.get("input_key") is not None
    has_text = arguments.get("text") is not None
    if has_key and has_text:
        raise ValueError("input_key 与 text 只能提供一个")
    if has_key:
        input_key = _required_text(arguments, "input_key", 100)
        if input_key not in task.inputs:
            raise ValueError(f"任务输入键不存在：{input_key}")
        return str(task.inputs[input_key]), input_key
    if not has_text:
        raise ValueError("必须提供 input_key（敏感值）或 text（非敏感字面量）之一")
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text 必须是非空字符串")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"text 不能超过 {MAX_TEXT_LENGTH} 个字符")
    if any(ord(char) < 32 and char not in "\n\t" for char in text) or "\x7f" in text:
        raise ValueError("text 不能包含控制字符")
    if any(text == str(value) for value in task.inputs.values()):
        raise ValueError("text 与某个任务输入的值相同；敏感值必须通过 input_key 引用")
    return text, None


def locator_recipe(arguments: Mapping[str, Any]) -> LocatorRecipe:
    raw = arguments.get("locator")
    if not isinstance(raw, Mapping):
        raise ValueError("locator 必须是对象")
    unknown = set(raw) - {
        "strategy",
        "value",
        "name",
        "exact",
        "index",
        "timeout_seconds",
        "frame_id",
    }
    if unknown:
        raise ValueError(f"locator 包含未知参数：{', '.join(sorted(unknown))}")
    strategy = _required_text(raw, "strategy", 20)
    if strategy not in {"css", "xpath", "role", "text", "label", "test_id"}:
        raise ValueError(f"不支持的定位策略：{strategy}")
    value = _required_text(raw, "value", 1024)
    name = str(raw.get("name", "")).strip()
    if len(name) > 300:
        raise ValueError("定位器名称不能超过 300 个字符")
    exact = raw.get("exact", strategy != "text")
    if not isinstance(exact, bool):
        raise ValueError("locator.exact 必须是布尔值")
    index_explicit = "index" in raw
    index = raw.get("index", 0)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 100:
        raise ValueError("locator.index 必须是 0 到 100 的整数")
    timeout_seconds = raw.get("timeout_seconds", 5)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not 0.1 <= float(timeout_seconds) <= 15
    ):
        raise ValueError("locator.timeout_seconds 必须在 0.1 到 15 秒之间")
    raw_frame_id = raw.get("frame_id")
    frame_id = _required_text(raw, "frame_id", 100) if raw_frame_id is not None else None
    config = {
        "value": value,
        "name": name,
        "exact": exact,
        "index": index,
        "index_explicit": index_explicit,
        "timeout_seconds": float(timeout_seconds),
    }
    return LocatorRecipe(
        strategy=f"explicit_{strategy}",
        value=json.dumps(config, ensure_ascii=False, separators=(",", ":")),
        role=value if strategy == "role" else None,
        name=name or None,
        frame_id=frame_id,
    )


def _required_text(arguments: Mapping[str, Any], key: str, maximum: int) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"定位器参数 {key} 不能为空")
    result = value.strip()
    if len(result) > maximum or any(ord(character) < 32 for character in result):
        raise ValueError(f"定位器参数 {key} 超出长度或包含控制字符")
    return result

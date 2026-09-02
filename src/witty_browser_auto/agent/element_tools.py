"""元素只读读取与页面控制工具：读取元素状态、派发功能键、执行页面历史导航。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from witty_browser_auto.agent.locator_tools import locator_recipe
from witty_browser_auto.browser.keyboard import resolve_key
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionKind,
    ExpectedCondition,
    LocatorRecipe,
    ModelToolCall,
)
from witty_browser_auto.domain.protocols import (
    AutomationDriver,
    ElementInspectionProvider,
    ElementScreenshotProvider,
)
from witty_browser_auto.security.redaction import redact_task_inputs, redact_url
from witty_browser_auto.toolkit.catalog import (
    ELEMENT_READ_TOOLS,
    PAGE_CONTROL_TOOLS,
    POINTER_TOOLS,
    names_of,
    schemas_of,
)

ELEMENT_READ_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = schemas_of(ELEMENT_READ_TOOLS)
ELEMENT_READ_TOOL_NAMES = names_of(ELEMENT_READ_TOOLS)
PAGE_CONTROL_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = schemas_of(PAGE_CONTROL_TOOLS)
PAGE_CONTROL_TOOL_NAMES = names_of(PAGE_CONTROL_TOOLS)
POINTER_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = schemas_of(POINTER_TOOLS)
POINTER_TOOL_NAMES = names_of(POINTER_TOOLS)

# 这些属性可能携带带令牌的地址，返回前统一走查询参数脱敏。
_URL_ATTRIBUTES = ("href", "src", "action")


@dataclass(frozen=True, slots=True)
class ElementReadOutcome:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


def element_inspection_available(driver: AutomationDriver) -> bool:
    return isinstance(driver, ElementInspectionProvider)


async def execute_element_read_tool(
    call: ModelToolCall,
    driver: AutomationDriver,
    *,
    task_inputs: Mapping[str, Any],
) -> ElementReadOutcome:
    """读取单个元素状态；页面执行模板固定在驱动层，调用方只能选择目标和上限。"""

    if call.name not in ELEMENT_READ_TOOL_NAMES:
        raise ValueError(f"未知元素读取工具：{call.name}")
    if call.name == "capture_element_screenshot":
        return await _capture_element_screenshot(call, driver)
    if not isinstance(driver, ElementInspectionProvider):
        return ElementReadOutcome(False, "当前浏览器表面没有元素读取能力")
    target_id, locator = _read_target(call.arguments)
    max_text_length = call.arguments.get("max_text_length", 2000)
    if isinstance(max_text_length, bool) or not isinstance(max_text_length, int):
        raise ValueError("max_text_length 必须是整数")
    include_html = call.arguments.get("include_html", False)
    if not isinstance(include_html, bool):
        raise ValueError("include_html 必须是布尔值")
    state = await driver.inspect_element(
        target_id=target_id,
        locator=locator,
        max_text_length=max_text_length,
        include_html=include_html,
    )
    safe = redact_task_inputs(_redact_attribute_urls(state), dict(task_inputs))
    return ElementReadOutcome(
        True,
        "元素状态已只读读取",
        safe if isinstance(safe, dict) else {},
    )


async def _capture_element_screenshot(
    call: ModelToolCall,
    driver: AutomationDriver,
) -> ElementReadOutcome:
    if not isinstance(driver, ElementScreenshotProvider):
        return ElementReadOutcome(False, "当前浏览器表面没有元素截图能力")
    target_id, locator = _read_target(call.arguments)
    label = call.arguments.get("label", "element")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("label 不能为空")
    padding = call.arguments.get("padding", 0)
    if isinstance(padding, bool) or not isinstance(padding, int | float):
        raise ValueError("padding 必须是数字")
    result = await driver.capture_element_screenshot(
        target_id=target_id,
        locator=locator,
        label=label.strip(),
        padding=float(padding),
    )
    return ElementReadOutcome(True, "元素截图已保存", dict(result))


def element_screenshot_available(driver: AutomationDriver) -> bool:
    return isinstance(driver, ElementScreenshotProvider)


def build_pointer_command(
    call: ModelToolCall,
    action_id: str,
    expected: ExpectedCondition | None,
) -> ActionCommand:
    """把悬停调用编译为动作命令；悬停不点击，但仍需业务后置条件证明它生效。"""

    if call.name != "hover":
        raise ValueError(f"不支持的指针工具：{call.name}")
    if expected is None:
        raise ValueError("悬停必须提供业务后置条件")
    target_id, locator = _read_target(call.arguments)
    return ActionCommand(
        action_id,
        ActionKind.HOVER,
        target_id=target_id,
        locator=locator,
        expected=expected,
        idempotent=True,
    )


def build_page_control_command(
    call: ModelToolCall,
    action_id: str,
    expected: ExpectedCondition | None,
) -> ActionCommand:
    """把按键和历史导航调用编译为确定性动作命令。"""

    if expected is None:
        raise ValueError("按键与历史导航都必须提供业务后置条件")
    if call.name == "press_key":
        return _build_press_key_command(call.arguments, action_id, expected)
    if call.name == "navigate_history":
        action = call.arguments.get("action")
        if action not in {"back", "forward", "reload"}:
            raise ValueError("页面历史动作必须是 back、forward 或 reload")
        return ActionCommand(
            action_id,
            ActionKind.NAVIGATE_HISTORY,
            value=action,
            expected=expected,
            timeout_seconds=30.0,
            idempotent=False,
        )
    raise ValueError(f"不支持的页面控制工具：{call.name}")


def _build_press_key_command(
    arguments: Mapping[str, Any],
    action_id: str,
    expected: ExpectedCondition,
) -> ActionCommand:
    key = arguments.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("按键工具必须提供 key")
    raw_modifiers = arguments.get("modifiers", ())
    if isinstance(raw_modifiers, str) or not isinstance(raw_modifiers, list | tuple):
        if raw_modifiers not in ((), None):
            raise ValueError("modifiers 必须是字符串数组")
        raw_modifiers = ()
    modifiers = tuple(str(item) for item in raw_modifiers)
    repeat = arguments.get("repeat", 1)
    if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 20:
        raise ValueError("repeat 必须是 1 到 20 的整数")
    resolved = resolve_key(key, modifiers)
    resolved["repeat"] = repeat
    target_id, locator = _optional_focus_target(arguments)
    return ActionCommand(
        action_id,
        ActionKind.PRESS_KEY,
        target_id=target_id,
        locator=locator,
        value=json.dumps(resolved, ensure_ascii=False, separators=(",", ":")),
        expected=expected,
        idempotent=False,
    )


def _read_target(arguments: Mapping[str, Any]) -> tuple[str | None, LocatorRecipe | None]:
    target_id = arguments.get("target_id")
    has_locator = arguments.get("locator") is not None
    if (target_id is None) == (not has_locator):
        raise ValueError("元素读取必须且只能提供 target_id 或 locator")
    if has_locator:
        return None, locator_recipe(arguments)
    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError("target_id 不能为空")
    return target_id, None


def _optional_focus_target(
    arguments: Mapping[str, Any],
) -> tuple[str | None, LocatorRecipe | None]:
    """按键可以作用于当前焦点，只有显式指定目标时才先聚焦。"""

    target_id = arguments.get("target_id")
    has_locator = arguments.get("locator") is not None
    if target_id is not None and has_locator:
        raise ValueError("按键工具只能提供 target_id 或 locator 之一")
    if has_locator:
        return None, locator_recipe(arguments)
    if target_id is None:
        return None, None
    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError("target_id 不能为空")
    return target_id, None


def _redact_attribute_urls(state: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(state)
    attributes = result.get("attributes")
    if not isinstance(attributes, Mapping):
        return result
    cleaned = dict(attributes)
    for key in _URL_ATTRIBUTES:
        value = cleaned.get(key)
        if isinstance(value, str) and value:
            cleaned[key] = redact_url(value)
    result["attributes"] = cleaned
    return result

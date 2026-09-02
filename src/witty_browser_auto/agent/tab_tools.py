"""标签页管理模型工具：列出、切换和关闭当前浏览器标签页。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from witty_browser_auto.agent.navigation_policy import assert_navigation_allowed
from witty_browser_auto.domain.models import ModelToolCall, TaskSpec
from witty_browser_auto.domain.protocols import AutomationDriver, TabManagementProvider
from witty_browser_auto.security.redaction import redact_task_inputs
from witty_browser_auto.toolkit.catalog import TAB_TOOLS, names_of, schemas_of

TAB_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = schemas_of(TAB_TOOLS)
TAB_TOOL_NAMES = names_of(TAB_TOOLS)


@dataclass(frozen=True, slots=True)
class TabToolOutcome:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    idempotent: bool = True
    counts_as_action: bool = True
    page_changed: bool = False


def tabs_available(driver: AutomationDriver) -> bool:
    return isinstance(driver, TabManagementProvider)


async def execute_tab_tool(
    call: ModelToolCall,
    driver: AutomationDriver,
    *,
    task: TaskSpec,
    task_inputs: dict[str, Any],
) -> TabToolOutcome:
    if call.name not in TAB_TOOL_NAMES:
        raise ValueError(f"未知标签页工具：{call.name}")
    if not isinstance(driver, TabManagementProvider):
        return TabToolOutcome(False, "当前浏览器表面没有标签页管理能力", counts_as_action=False)
    if call.name == "open_tab":
        url = call.arguments.get("url")
        unknown = set(call.arguments) - {"url"}
        if unknown:
            raise ValueError(f"新建标签页包含未知参数：{', '.join(sorted(unknown))}")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("新建标签页必须提供 url")
        # 新页面和 navigate 走同一条授权域名判定，避免绕过导航范围。
        assert_navigation_allowed(task, url.strip())
        result = await driver.open_tab(url.strip())
        safe = redact_task_inputs(dict(result), task_inputs)
        return TabToolOutcome(
            True,
            "新标签页已打开并切换，请重新观察当前页面",
            safe if isinstance(safe, dict) else {},
            idempotent=False,
            counts_as_action=True,
            page_changed=True,
        )
    if call.name == "list_tabs":
        if call.arguments:
            raise ValueError("标签页列举不接受任何参数")
        tabs = await driver.list_tabs()
        safe = redact_task_inputs({"tabs": tabs[:50], "tab_count": len(tabs)}, task_inputs)
        return TabToolOutcome(
            True,
            "标签页已完成只读列举",
            safe if isinstance(safe, dict) else {},
            idempotent=True,
            counts_as_action=False,
        )
    target_id = _required_target_id(call.arguments)
    if call.name == "switch_tab":
        result = await driver.switch_tab(target_id)
        safe = redact_task_inputs(dict(result), task_inputs)
        switched = bool(result.get("switched"))
        return TabToolOutcome(
            True,
            "已切换到目标标签页，请重新观察当前页面" if switched else "目标已是当前标签页",
            safe if isinstance(safe, dict) else {},
            idempotent=True,
            counts_as_action=switched,
            page_changed=switched,
        )
    result = await driver.close_tab(target_id)
    safe = redact_task_inputs(dict(result), task_inputs)
    was_current = bool(result.get("was_current"))
    return TabToolOutcome(
        True,
        "任务标签页已关闭；当前页已切换，请重新观察" if was_current else "任务标签页已关闭",
        safe if isinstance(safe, dict) else {},
        idempotent=False,
        counts_as_action=True,
        page_changed=was_current,
    )


def _required_target_id(arguments: dict[str, Any]) -> str:
    unknown = set(arguments) - {"target_id"}
    if unknown:
        raise ValueError(f"标签页工具包含未知参数：{', '.join(sorted(unknown))}")
    value = arguments.get("target_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("标签页工具必须提供 target_id")
    target_id = value.strip()
    if len(target_id) > 80 or any(ord(character) < 32 for character in target_id):
        raise ValueError("标签页 target_id 超出长度或包含控制字符")
    return target_id

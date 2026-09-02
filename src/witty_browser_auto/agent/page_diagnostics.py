"""页面故障诊断模型工具与失败上下文回灌。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from witty_browser_auto.domain.models import ModelToolCall
from witty_browser_auto.domain.protocols import AutomationDriver, PageDiagnosticsProvider
from witty_browser_auto.security.redaction import redact_task_inputs
from witty_browser_auto.toolkit.catalog import DIAGNOSTIC_TOOLS, schemas_of

logger = logging.getLogger(__name__)

PAGE_DIAGNOSTIC_TOOL_NAME = DIAGNOSTIC_TOOLS[0].name
PAGE_DIAGNOSTIC_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = schemas_of(DIAGNOSTIC_TOOLS)


@dataclass(frozen=True, slots=True)
class PageDiagnosticOutcome:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


def diagnostics_available(driver: AutomationDriver) -> bool:
    return isinstance(driver, PageDiagnosticsProvider)


async def execute_page_diagnostic_tool(
    call: ModelToolCall,
    driver: AutomationDriver,
    *,
    task_inputs: dict[str, Any],
) -> PageDiagnosticOutcome:
    if call.name != PAGE_DIAGNOSTIC_TOOL_NAME:
        raise ValueError(f"未知页面诊断工具：{call.name}")
    unknown = set(call.arguments) - {"max_console", "max_network"}
    if unknown:
        raise ValueError(f"页面诊断包含未知参数：{', '.join(sorted(unknown))}")
    max_console = _bounded_integer(call.arguments.get("max_console", 20), "max_console", 1, 50)
    max_network = _bounded_integer(call.arguments.get("max_network", 30), "max_network", 1, 100)
    if not isinstance(driver, PageDiagnosticsProvider):
        return PageDiagnosticOutcome(False, "当前浏览器表面没有页面诊断能力")
    result = await driver.diagnostic_snapshot(
        max_console=max_console,
        max_network=max_network,
    )
    safe = redact_task_inputs(result, task_inputs)
    return PageDiagnosticOutcome(
        True,
        "页面运行时与网络故障信号已完成只读采样",
        safe if isinstance(safe, dict) else {},
    )


async def enrich_failure_data(
    driver: AutomationDriver,
    task_inputs: dict[str, Any],
    base: dict[str, Any],
) -> dict[str, Any]:
    """动作失败时立即采样，避免非幂等动作停止后丢失现场。"""

    data = dict(base)
    if not isinstance(driver, PageDiagnosticsProvider):
        return data
    try:
        snapshot = await driver.diagnostic_snapshot(max_console=12, max_network=30)
    except Exception as exc:
        logger.warning(
            "动作失败后的页面诊断采样失败",
            extra={"exception_type": type(exc).__name__},
        )
        return data
    safe = redact_task_inputs(snapshot, task_inputs)
    if isinstance(safe, dict):
        data["页面诊断"] = safe
    return data


def _bounded_integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value

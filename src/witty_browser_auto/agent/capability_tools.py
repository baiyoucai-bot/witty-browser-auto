"""执行模型上报现有工具能力缺口时使用的只读协议。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from witty_browser_auto.domain.models import TaskSpec
from witty_browser_auto.security.redaction import redact_task_inputs
from witty_browser_auto.toolkit.catalog import CAPABILITY_AREAS, CAPABILITY_TOOLS, schemas_of

CAPABILITY_GAP_TOOL_NAME = CAPABILITY_TOOLS[0].name
_CAPABILITY_AREAS = frozenset(CAPABILITY_AREAS)
_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)

CAPABILITY_GAP_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = schemas_of(CAPABILITY_TOOLS)


def capability_area_for_tool(tool_name: str) -> str:
    if tool_name in {"inspect_collection_structure", "run_structured_extraction"}:
        return "structured_extraction"
    if tool_name in {
        "inspect_network_data",
        "export_network_response",
        "manage_network_route",
    }:
        return "network_data"
    if tool_name.endswith("_locator"):
        return "locator"
    return "browser_action"


@dataclass(frozen=True, slots=True)
class CapabilityGapReport:
    message: str
    data: dict[str, Any]


def build_capability_gap_report(
    arguments: Mapping[str, Any],
    task: TaskSpec,
) -> CapabilityGapReport:
    unknown = set(arguments) - {"area", "capability", "evidence", "related_tool"}
    if unknown:
        raise ValueError(f"能力缺口报告包含未知参数：{', '.join(sorted(unknown))}")
    area = _required_text(arguments, "area", 40)
    if area not in _CAPABILITY_AREAS:
        raise ValueError(f"不支持的能力缺口领域：{area}")
    capability = _safe_description(arguments, "capability", task)
    evidence = _safe_description(arguments, "evidence", task)
    related_tool = _optional_text(arguments, "related_tool", 100)
    related = f"；关联工具：{related_tool}" if related_tool else ""
    return CapabilityGapReport(
        message=f"已记录现有工具能力缺口：{capability}；现场证据：{evidence}{related}",
        data={
            "capability_gap": {
                "area": area,
                "capability": capability,
                "evidence": evidence,
                "related_tool": related_tool,
            }
        },
    )


def _safe_description(arguments: Mapping[str, Any], key: str, task: TaskSpec) -> str:
    value = _required_text(arguments, key, 600)
    redacted = redact_task_inputs(value, task.inputs)
    return _URL.sub("[URL]", str(redacted))


def _required_text(arguments: Mapping[str, Any], key: str, maximum: int) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"能力缺口参数 {key} 不能为空")
    result = value.strip()
    if len(result) > maximum or any(ord(character) < 32 for character in result):
        raise ValueError(f"能力缺口参数 {key} 超出长度或包含控制字符")
    return result


def _optional_text(arguments: Mapping[str, Any], key: str, maximum: int) -> str:
    if arguments.get(key) is None:
        return ""
    return _required_text(arguments, key, maximum)

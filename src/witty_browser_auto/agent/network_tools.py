"""网络数据工具的独立 schema、参数校验和安全摘要。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from witty_browser_auto.domain.errors import PolicyViolationError
from witty_browser_auto.domain.models import EvidenceRef
from witty_browser_auto.domain.network_data import (
    NetworkDataExportResult,
    sanitize_network_inspection,
)
from witty_browser_auto.domain.protocols import NetworkDataExtractor
from witty_browser_auto.security.redaction import redact_task_inputs
from witty_browser_auto.toolkit.catalog import NETWORK_TOOLS, names_of, schemas_of

_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "api-key",
        "authorization",
        "cookie",
        "host",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
    }
)

NETWORK_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = schemas_of(NETWORK_TOOLS)
NETWORK_TOOL_NAMES = names_of(NETWORK_TOOLS)


@dataclass(frozen=True, slots=True)
class NetworkToolOutcome:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    evidence: EvidenceRef | None = None
    export_result: NetworkDataExportResult | None = None
    idempotent: bool = True
    counts_as_action: bool = True


async def execute_network_tool(
    name: str,
    arguments: Mapping[str, Any],
    extractor: NetworkDataExtractor | None,
    *,
    task_inputs: Mapping[str, Any],
) -> NetworkToolOutcome:
    if extractor is None:
        raise PolicyViolationError("当前任务未启用网络响应体数据能力")
    if name == "inspect_network_data":
        unknown = set(arguments) - {"max_candidates"}
        if unknown:
            raise ValueError(f"网络数据观察包含未知参数：{', '.join(sorted(unknown))}")
        maximum = arguments.get("max_candidates", 20)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 50:
            raise ValueError("网络接口候选数量必须在 1 到 50 之间")
        raw = await extractor.inspect(max_candidates=maximum)
        safe = sanitize_network_inspection(raw, max_candidates=maximum)
        redacted = redact_task_inputs(safe, task_inputs)
        return NetworkToolOutcome(
            success=True,
            message="网络接口候选已由代码完成只读分析，请选择候选后导出",
            data=redacted if isinstance(redacted, dict) else {},
        )
    if name == "export_network_response":
        unknown = set(arguments) - {"candidate_id", "candidate_ids", "collection_name"}
        if unknown:
            raise ValueError(f"网络数据导出包含未知参数：{', '.join(sorted(unknown))}")
        candidate_ids = _candidate_ids(arguments)
        collection_name = _required_text(arguments, "collection_name", 100)
        if len(candidate_ids) == 1:
            result = await extractor.export(candidate_ids[0], collection_name)
        else:
            export_many = getattr(extractor, "export_many", None)
            if export_many is None:
                raise ValueError("当前网络提取器不支持批量候选导出")
            result = await export_many(candidate_ids, collection_name)
        safe_summary = redact_task_inputs(result.model_summary(), task_inputs)
        return NetworkToolOutcome(
            success=True,
            message=(
                "网络数据已完成全部分页校验并由代码导出"
                if result.has_strong_completion_evidence
                else (
                    f"已聚合 {result.captured_response_count} 个网络响应，"
                    "但总数或页数尚未闭合，不能作为全部数据的完成证据"
                )
            ),
            data=safe_summary if isinstance(safe_summary, dict) else {},
            evidence=EvidenceRef(
                evidence_id=f"network-data-{result.candidate_id}",
                kind="network_response_json",
                path=str(result.json_path),
                summary=(
                    f"{result.collection_name}网络响应代码导出"
                    + (
                        f"，识别 {result.record_count} 条记录"
                        if result.record_count is not None
                        else ""
                    )
                ),
            ),
            export_result=result,
        )
    if name == "wait_network_response":
        unknown = set(arguments) - {"url_substring", "timeout_seconds"}
        if unknown:
            raise ValueError(f"等待网络响应包含未知参数：{', '.join(sorted(unknown))}")
        substring = _required_text(arguments, "url_substring", 500)
        timeout = arguments.get("timeout_seconds", 30)
        timeout_valid = not isinstance(timeout, bool) and isinstance(timeout, (int, float))
        if not timeout_valid or not 1 <= timeout <= 300:
            raise ValueError("等待网络响应的超时必须在 1 到 300 秒之间")
        waiter = getattr(extractor, "wait_for_response", None)
        if waiter is None:
            raise ValueError("当前网络提取器不支持等待网络响应")
        result = await waiter(substring, timeout_seconds=float(timeout))
        safe = redact_task_inputs(dict(result), task_inputs)
        matched = bool(result.get("matched"))
        captured = bool(result.get("captured"))
        if matched and captured:
            message = "匹配的网络响应已捕获，可通过网络候选检查后选择该 candidate_id 导出"
        elif matched:
            message = "匹配的网络请求已完成，但响应未进入 JSON 捕获，只有元数据可用"
        else:
            message = "等待网络响应超时，指定接口在窗口内没有返回"
        return NetworkToolOutcome(
            success=True,
            message=message,
            data=safe if isinstance(safe, dict) else {},
            idempotent=True,
            counts_as_action=False,
        )
    if name == "manage_network_route":
        unknown = set(arguments) - {
            "operation",
            "rule_id",
            "url_pattern",
            "action",
            "method",
            "request_headers",
            "request_header_input_keys",
            "request_method",
            "request_body",
            "response_status",
            "response_headers",
            "response_header_input_keys",
            "response_body",
        }
        if unknown:
            raise ValueError(f"网络路由包含未知参数：{', '.join(sorted(unknown))}")
        operation = _required_text(arguments, "operation", 10)
        if operation not in {"list", "add", "remove"}:
            raise ValueError("网络路由 operation 必须是 list、add 或 remove")
        manager = getattr(extractor, "manage_route", None)
        if manager is None:
            raise ValueError("当前网络提取器不支持网络路由管理")
        config = {key: value for key, value in arguments.items() if key != "operation"}
        if operation == "add":
            config["request_headers"] = _resolved_headers(
                arguments.get("request_headers"),
                arguments.get("request_header_input_keys"),
                task_inputs,
                field="request_headers",
            )
            config["response_headers"] = _resolved_headers(
                arguments.get("response_headers"),
                arguments.get("response_header_input_keys"),
                task_inputs,
                field="response_headers",
            )
        config.pop("request_header_input_keys", None)
        config.pop("response_header_input_keys", None)
        if not config.get("request_headers"):
            config.pop("request_headers", None)
        if not config.get("response_headers"):
            config.pop("response_headers", None)
        result = await manager(operation, config)
        safe = redact_task_inputs(dict(result), task_inputs)
        return NetworkToolOutcome(
            success=True,
            message=(
                "网络路由已完成只读检查"
                if operation == "list"
                else ("网络路由已安装" if operation == "add" else "网络路由已移除")
            ),
            data=safe if isinstance(safe, dict) else {},
            idempotent=operation == "list",
            counts_as_action=operation != "list",
        )
    raise ValueError(f"不支持的网络数据工具：{name}")


def _candidate_ids(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    single = arguments.get("candidate_id")
    multiple = arguments.get("candidate_ids")
    if single is not None and multiple is not None:
        raise ValueError("candidate_id 与 candidate_ids 只能填写一个")
    if single is not None:
        return (_required_text(arguments, "candidate_id", 80),)
    if not isinstance(multiple, list) or not 1 <= len(multiple) <= 50:
        raise ValueError("网络数据导出必须提供 1 到 50 个 candidate_ids")
    candidate_ids = tuple(
        _required_text({"candidate_id": value}, "candidate_id", 80) for value in multiple
    )
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate_ids 不能包含重复值")
    return candidate_ids


def _required_text(arguments: Mapping[str, Any], key: str, maximum: int) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"网络数据参数 {key} 不能为空")
    result = value.strip()
    if len(result) > maximum or any(ord(character) < 32 for character in result):
        raise ValueError(f"网络数据参数 {key} 超出长度或包含控制字符")
    return result


def _resolved_headers(
    literal_headers: Any,
    input_key_headers: Any,
    task_inputs: Mapping[str, Any],
    *,
    field: str,
) -> dict[str, str]:
    literals = _header_mapping(literal_headers, field)
    references = _header_mapping(input_key_headers, f"{field[:-1]}_input_keys")
    literal_names = {name.casefold() for name in literals}
    reference_names = {name.casefold() for name in references}
    if duplicates := literal_names.intersection(reference_names):
        raise ValueError(f"Header 不能同时提供字面值和 input_key：{', '.join(sorted(duplicates))}")
    for name in literals:
        if _is_sensitive_header(name):
            raise ValueError(f"敏感 Header {name} 必须通过任务 input_key 注入")

    resolved = dict(literals)
    for name, input_key in references.items():
        if input_key not in task_inputs:
            raise ValueError(f"Header {name} 引用的任务输入键不存在：{input_key}")
        value = task_inputs[input_key]
        if not isinstance(value, str) or not value:
            raise ValueError(f"Header {name} 引用的任务输入必须是非空文本：{input_key}")
        if "\r" in value or "\n" in value:
            raise ValueError(f"Header {name} 的任务输入包含非法换行")
        resolved[name] = value
    return resolved


def _header_mapping(value: Any, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"网络路由参数 {field} 必须是对象")
    result: dict[str, str] = {}
    normalized_names: set[str] = set()
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError(f"网络路由参数 {field} 包含无效 Header 名")
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValueError(f"网络路由参数 {field} 的值必须是非空文本")
        name = raw_name.strip()
        if name.casefold() in normalized_names:
            raise ValueError(f"网络路由参数 {field} 包含重复 Header：{name}")
        normalized_names.add(name.casefold())
        result[name] = raw_value.strip()
    return result


def _is_sensitive_header(name: str) -> bool:
    normalized = name.casefold()
    return (
        normalized in _SENSITIVE_HEADER_NAMES
        or "token" in normalized
        or "secret" in normalized
        or "credential" in normalized
        or normalized.endswith("-key")
    )

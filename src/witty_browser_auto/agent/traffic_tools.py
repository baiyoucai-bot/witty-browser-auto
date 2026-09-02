"""流量检查工具的执行分支：清单、正文、HAR 与重放。

每个分支同时产出调用方视图与模型视图；模型视图只保留边界信息，不含正文与 Header 值。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from witty_browser_auto.domain.errors import PolicyViolationError
from witty_browser_auto.domain.models import EvidenceRef
from witty_browser_auto.network.inspection import NetworkTrafficInspector
from witty_browser_auto.security.redaction import redact_task_inputs
from witty_browser_auto.toolkit.catalog import TRAFFIC_TOOLS, names_of, schemas_of

TRAFFIC_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = schemas_of(TRAFFIC_TOOLS)
TRAFFIC_TOOL_NAMES = names_of(TRAFFIC_TOOLS)


@dataclass(frozen=True, slots=True)
class TrafficToolOutcome:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    model_data: dict[str, Any] = field(default_factory=dict)
    evidence: EvidenceRef | None = None
    idempotent: bool = True
    counts_as_action: bool = False


async def execute_traffic_tool(
    name: str,
    arguments: Mapping[str, Any],
    inspector: NetworkTrafficInspector | None,
    *,
    task_inputs: Mapping[str, Any],
) -> TrafficToolOutcome:
    if inspector is None:
        raise PolicyViolationError("当前任务未启用流量检查能力")
    if name == "inspect_network_traffic":
        _reject_unknown(
            name,
            arguments,
            {
                "url_contains",
                "methods",
                "resource_types",
                "status_min",
                "status_max",
                "only_failed",
                "limit",
            },
        )
        full, model = await inspector.inspect(arguments)
        return TrafficToolOutcome(
            success=True,
            message=(
                f"已列出 {full['returned_count']} 条网络交换，缓冲区共 {full['exchange_count']} 条"
            ),
            data=full,
            model_data=_safe(model, task_inputs),
        )
    if name == "read_network_body":
        _reject_unknown(name, arguments, {"exchange_id", "part"})
        full, model = await inspector.read_body(arguments)
        return TrafficToolOutcome(
            success=True,
            message=(
                f"已读取交换 {full['exchange_id']} 的{full['part']}正文，"
                f"共 {full['byte_length']} 字节"
                if full.get("available")
                else f"该正文不可用：{full.get('reason', '未知原因')}"
            ),
            data=full,
            model_data=_safe(model, task_inputs),
        )
    if name == "search_network_traffic":
        _reject_unknown(
            name,
            arguments,
            {"query", "scope", "case_sensitive", "url_contains", "resource_types", "limit"},
        )
        full, model = await inspector.search(arguments)
        return TrafficToolOutcome(
            success=full["returned_count"] > 0,
            message=(
                f"在 {full['scope']} 范围内命中 {full['returned_count']} 次交换"
                if full["returned_count"]
                else f"在 {full['scope']} 范围内没有匹配 {full['query']!r} 的交换"
            ),
            data=full,
            model_data=_safe(model, task_inputs),
        )
    if name == "export_network_har":
        _reject_unknown(
            name,
            arguments,
            {
                "url_contains",
                "methods",
                "resource_types",
                "status_min",
                "status_max",
                "only_failed",
                "limit",
                "collection_name",
                "include_bodies",
            },
        )
        full, model = await inspector.export_har(arguments)
        return TrafficToolOutcome(
            success=True,
            message=(
                f"已导出 {full['entry_count']} 条 HTTP 交换和 "
                f"{full['websocket_count']} 个 WebSocket 连接到 HAR"
            ),
            data=full,
            model_data=_safe(model, task_inputs),
            evidence=EvidenceRef(
                evidence_id=f"network-har-{full['byte_count']}",
                kind="network_har",
                path=str(full["har_path"]),
                summary=f"HAR 导出，含 {full['entry_count']} 条交换",
            ),
        )
    if name == "replay_network_request":
        _reject_unknown(
            name,
            arguments,
            {
                "exchange_id",
                "url",
                "method",
                "headers",
                "remove_headers",
                "body",
                "referrer",
            },
        )
        full, model = await inspector.replay(arguments)
        succeeded = bool(full.get("success"))
        return TrafficToolOutcome(
            success=succeeded,
            message=(
                f"重放完成，服务端返回 {full.get('status')} {full.get('status_text', '')}".strip()
                if succeeded
                else f"重放未能完成：{full.get('error', '未知原因')}"
            ),
            data=full,
            model_data=_safe(model, task_inputs),
            idempotent=False,
            counts_as_action=True,
        )
    if name == "read_websocket_frames":
        _reject_unknown(name, arguments, {"exchange_id", "direction", "contains", "limit"})
        full, model = await inspector.read_websocket_frames(arguments)
        return TrafficToolOutcome(
            success=True,
            message=(
                f"读取到 {full['returned_count']} 帧，"
                f"该连接共 {full['frame_count']} 帧、{full['total_bytes']} 字节"
            ),
            data=full,
            model_data=_safe(model, task_inputs),
        )
    if name == "read_sse_messages":
        _reject_unknown(name, arguments, {"exchange_id", "event_name", "contains", "limit"})
        full, model = await inspector.read_sse(arguments)
        return TrafficToolOutcome(
            success=True,
            message=(
                f"读取到 {full['returned_count']} 条 SSE 消息，"
                f"该连接共 {full['message_count']} 条、{full['total_bytes']} 字节"
            ),
            data=full,
            model_data=_safe(model, task_inputs),
        )
    if name == "analyze_api_endpoint":
        _reject_unknown(name, arguments, {"exchange_id", "url_contains"})
        full, model = await inspector.analyze_api(arguments)
        endpoint = full["endpoint"]
        return TrafficToolOutcome(
            success=True,
            message=(
                f"已归纳接口 {endpoint['method']} {endpoint['url_template']}，"
                f"基于 {full['sample_count']} 次交换"
            ),
            data=full,
            model_data=_safe(model, task_inputs),
        )
    if name == "collect_api_pages":
        _reject_unknown(
            name,
            arguments,
            {
                "exchange_id",
                "url_contains",
                "strategy",
                "page_param",
                "page_in",
                "cursor_in",
                "cursor_header",
                "start",
                "step",
                "page_size",
                "record_path",
                "total_path",
                "cursor_field",
                "dedupe_key",
                "max_pages",
                "delay_ms",
            },
        )
        full, model = await inspector.collect_pages(arguments)
        closed = bool(full.get("closed"))
        return TrafficToolOutcome(
            # 未闭合就是没取全，必须以失败回报，否则"抓了一些"会被当成"抓全了"。
            success=closed,
            message=(
                f"已取全 {full['collected']} 条，翻了 {full['pages_fetched']} 页：{full['reason']}"
                if closed
                else f"未能确认取全，已收 {full['collected']} 条：{full['reason']}"
            ),
            data=full,
            model_data=_safe(model, task_inputs),
            idempotent=False,
        )
    if name == "export_request_code":
        _reject_unknown(name, arguments, {"exchange_id", "target", "include_secrets"})
        full, model = await inspector.export_code(arguments)
        placeholders = full.get("placeholders", [])
        return TrafficToolOutcome(
            success=True,
            message=(
                f"已生成 {full['target']} 代码，共 {len(full['code'])} 字符"
                + (f"，{len(placeholders)} 个凭据已替换为环境变量占位" if placeholders else "")
            ),
            data=full,
            model_data=_safe(model, task_inputs),
        )
    raise ValueError(f"不支持的流量检查工具：{name}")


def _reject_unknown(name: str, arguments: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(arguments) - allowed
    if unknown:
        raise ValueError(f"{name} 包含未知参数：{', '.join(sorted(unknown))}")


def _safe(payload: Mapping[str, Any], task_inputs: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_task_inputs(dict(payload), task_inputs)
    return redacted if isinstance(redacted, dict) else {}

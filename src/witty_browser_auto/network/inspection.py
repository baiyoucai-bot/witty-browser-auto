"""流量检查门面：清单、正文读取、HAR 导出与请求重放的唯一入口。

同时产出两份视图：`data` 给外部代码调用方，`model_data` 给智能体循环里的模型。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.config import NetworkTrafficConfig
from witty_browser_auto.network.api_analysis import (
    analyze_endpoint,
    endpoint_signature,
    model_view,
)
from witty_browser_auto.network.codegen import build_request_code
from witty_browser_auto.network.har import build_har
from witty_browser_auto.network.pagination import (
    DEFAULT_MAX_PAGES,
    MAX_PAGES,
    MAX_RECORDS,
    CollectionOutcome,
    PageAttempt,
    build_plan,
    decide_closure,
    extract_cursor,
    extract_cursor_from_headers,
    extract_next_link,
    extract_records,
    extract_total,
    page_body,
    page_url,
    record_fingerprint,
)
from witty_browser_auto.network.replay import ReplayResult, build_replay_request, perform_replay
from witty_browser_auto.network.routing import CdpNetworkRouter
from witty_browser_auto.network.traffic import (
    NetworkExchange,
    NetworkTrafficLog,
    ServerSentEvent,
    WebSocketFrame,
)
from witty_browser_auto.security.redaction import redact_url

logger = logging.getLogger(__name__)

_COLLECTION_NAME = re.compile(r"^[\w\u4e00-\u9fff-]{1,100}$", re.UNICODE)
_MAX_LISTING = 200
_MODEL_LISTING_CAP = 30
_MAX_FRAMES = 500


def _frame_summary(frames: Sequence[WebSocketFrame]) -> dict[str, Any]:
    directions: dict[str, int] = {}
    opcodes: dict[str, int] = {}
    for frame in frames:
        directions[frame.direction] = directions.get(frame.direction, 0) + 1
        opcodes[frame.opcode] = opcodes.get(frame.opcode, 0) + 1
    return {
        "frame_count": len(frames),
        "directions": directions,
        "opcodes": opcodes,
        "total_bytes": sum(frame.byte_length for frame in frames),
        "truncated_count": sum(1 for frame in frames if frame.truncated),
    }


def _sse_summary(messages: Sequence[ServerSentEvent]) -> dict[str, Any]:
    events: dict[str, int] = {}
    for message in messages:
        events[message.event] = events.get(message.event, 0) + 1
    return {
        "message_count": len(messages),
        "events": events,
        "total_bytes": sum(message.byte_length for message in messages),
        "truncated_count": sum(1 for message in messages if message.truncated),
    }


@dataclass(frozen=True, slots=True)
class TrafficSessionContext:
    """重放需要的页面现场；由驱动在每次调用时提供。"""

    session: CdpTargetSession
    router: CdpNetworkRouter | None
    page_url: str


class NetworkTrafficInspector:
    """持有流量日志与产物目录；重放时向驱动索取当前页面会话。"""

    def __init__(
        self,
        log: NetworkTrafficLog,
        artifact_root: Path,
        *,
        config: NetworkTrafficConfig,
        allowed_origins: Sequence[str] = (),
    ) -> None:
        self.log = log
        self.artifact_root = artifact_root
        self.config = config
        self.allowed_origins = tuple(allowed_origins)
        self._session_source: Callable[[], TrafficSessionContext | None] | None = None

    def bind_session_source(
        self,
        source: Callable[[], TrafficSessionContext | None],
    ) -> None:
        self._session_source = source

    # ------------------------------------------------------------------
    # 清单
    # ------------------------------------------------------------------

    async def inspect(self, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        limit = _bounded_int(arguments.get("limit"), "limit", 1, _MAX_LISTING, default=50)
        selected = self.log.select(
            url_contains=_optional_text(arguments.get("url_contains"), "url_contains", 500) or "",
            methods=_string_list(arguments.get("methods"), "methods"),
            resource_types=_string_list(arguments.get("resource_types"), "resource_types"),
            status_min=_bounded_int(
                arguments.get("status_min"), "status_min", 100, 599, default=None
            ),
            status_max=_bounded_int(
                arguments.get("status_max"), "status_max", 100, 599, default=None
            ),
            only_failed=_bool(arguments.get("only_failed"), "only_failed"),
            limit=limit,
        )
        stats = self.log.stats()
        full = {
            "exchanges": [item.full_dict() for item in selected],
            "returned_count": len(selected),
            **stats,
        }
        model = {
            "exchanges": [item.model_dict() for item in selected[-_MODEL_LISTING_CAP:]],
            "returned_count": len(selected),
            "model_visible_count": min(len(selected), _MODEL_LISTING_CAP),
            **stats,
        }
        return full, model

    # ------------------------------------------------------------------
    # 全文搜索
    # ------------------------------------------------------------------

    async def search(self, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """在已抓取的正文/头/帧/SSE 里按子串搜索，定位数据来自哪次交换。"""

        query = _optional_text(arguments.get("query"), "query", 500)
        if query is None:
            raise ValueError("必须提供 query")
        scope = _optional_text(arguments.get("scope"), "scope", 20) or "body"
        matches = self.log.search(
            query=query,
            scope=scope,
            case_sensitive=_bool(arguments.get("case_sensitive"), "case_sensitive"),
            url_contains=_optional_text(arguments.get("url_contains"), "url_contains", 500) or "",
            resource_types=_string_list(arguments.get("resource_types"), "resource_types"),
            limit=_bounded_int(arguments.get("limit"), "limit", 1, _MAX_LISTING, default=50),
        )
        full = {
            "query": query,
            "scope": scope,
            "returned_count": len(matches),
            "matches": [
                {
                    "exchange_id": match.exchange.exchange_id,
                    "url": match.exchange.url,
                    "method": match.exchange.method,
                    "status": match.exchange.status,
                    "resource_type": match.exchange.resource_type,
                    "part": match.part,
                    "field_name": match.field_name,
                    "match_count": match.match_count,
                    "snippet": match.snippet,
                }
                for match in matches
            ],
        }
        model = {
            "query": query,
            "scope": scope,
            "returned_count": len(matches),
            "matches": [
                {
                    "exchange_id": match.exchange.exchange_id,
                    "url": redact_url(match.exchange.url),
                    "part": match.part,
                    "match_count": match.match_count,
                }
                for match in matches
            ],
            "note": "命中片段只返回给调用方进程，模型侧只提供交换定位与命中次数",
        }
        return full, model

    # ------------------------------------------------------------------
    # 正文
    # ------------------------------------------------------------------

    async def read_body(
        self,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        exchange = self._require_exchange(arguments.get("exchange_id"))
        part = _optional_text(arguments.get("part"), "part", 20) or "response"
        if part not in {"request", "response"}:
            raise ValueError("part 必须是 request 或 response")
        body = exchange.request_body if part == "request" else exchange.response_body
        if body is None:
            raise ValueError(f"交换 {exchange.exchange_id} 没有可读的{part}正文")
        if body.text is None and body.spill_path:
            # 正文太大没进内存，但已落盘；给出路径比报"不可用"有用得多。
            full = {
                "exchange_id": exchange.exchange_id,
                "part": part,
                "url": exchange.url,
                "method": exchange.method,
                "status": exchange.status,
                "mime_type": exchange.mime_type,
                **body.public_dict(),
                "text": None,
            }
            model = {
                "exchange_id": exchange.exchange_id,
                "part": part,
                "available": False,
                "byte_length": body.byte_length,
                "truncated": body.truncated,
                "reason": body.reason,
                "note": "正文已落盘，路径只返回给调用方进程",
            }
            return full, model
        full = {
            "exchange_id": exchange.exchange_id,
            "part": part,
            "url": exchange.url,
            "method": exchange.method,
            "status": exchange.status,
            "mime_type": exchange.mime_type,
            **body.public_dict(),
            "text": body.text,
        }
        parsed = _parse_json(body.text) if body.text is not None else None
        if parsed is not None:
            full["json"] = parsed
        model = {
            "exchange_id": exchange.exchange_id,
            "part": part,
            "available": body.available,
            "byte_length": body.byte_length,
            "truncated": body.truncated,
            "reason": body.reason,
            "note": "正文只返回给调用方进程，模型侧只提供长度与可用性",
        }
        return full, model

    # ------------------------------------------------------------------
    # WebSocket 帧
    # ------------------------------------------------------------------

    async def read_websocket_frames(
        self,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """读取某个 WebSocket 连接的帧内容。

        帧不是请求体也不是响应体，`read_network_body` 对 WebSocket 交换必然落空，
        因此实时推送类接口需要这条独立入口才能拿到内容。
        """

        exchange = self._require_exchange(arguments.get("exchange_id"))
        if not exchange.is_websocket:
            raise ValueError(f"交换 {exchange.exchange_id} 不是 WebSocket 连接")
        direction = _optional_text(arguments.get("direction"), "direction", 20)
        if direction is not None and direction not in {"sent", "received"}:
            raise ValueError("direction 必须是 sent 或 received")
        contains = _optional_text(arguments.get("contains"), "contains", 500)
        limit = _bounded_int(arguments.get("limit"), "limit", 1, _MAX_FRAMES, default=100)

        frames = exchange.websocket_frames
        matched = [
            frame
            for frame in frames
            if (direction is None or frame.direction == direction)
            and (contains is None or contains in frame.payload)
        ]
        # 实时连接的帧数远超单次可读上限，保留最新的一段比保留最早的更有用。
        selected = matched[-limit:] if limit is not None else matched
        rendered = []
        for frame in selected:
            item = {**frame.public_dict(), "payload": frame.payload}
            parsed = _parse_json(frame.payload)
            if parsed is not None:
                item["json"] = parsed
            rendered.append(item)

        summary = _frame_summary(frames)
        full = {
            "exchange_id": exchange.exchange_id,
            "url": exchange.url,
            "state": exchange.state,
            "frames": rendered,
            "returned_count": len(rendered),
            "matched_count": len(matched),
            **summary,
        }
        model = {
            "exchange_id": exchange.exchange_id,
            "url": redact_url(exchange.url),
            "state": exchange.state,
            "returned_count": len(rendered),
            "matched_count": len(matched),
            **summary,
            "note": "帧内容只返回给调用方进程，模型侧只提供方向、类型与字节统计",
        }
        return full, model

    # ------------------------------------------------------------------
    # SSE 消息
    # ------------------------------------------------------------------

    async def read_sse(self, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """读取某个 `text/event-stream` 连接已收到的消息。

        SSE 连接常年不关闭，`read_network_body` 读不到内容；LLM 对话、通知推送这类
        流式接口只能用本入口。
        """

        exchange = self._require_exchange(arguments.get("exchange_id"))
        if not exchange.is_event_source:
            raise ValueError(f"交换 {exchange.exchange_id} 不是 SSE 连接")
        event_name = _optional_text(arguments.get("event_name"), "event_name", 200)
        contains = _optional_text(arguments.get("contains"), "contains", 500)
        limit = _bounded_int(arguments.get("limit"), "limit", 1, _MAX_FRAMES, default=100)

        messages = exchange.sse_messages
        matched = [
            message
            for message in messages
            if (event_name is None or message.event == event_name)
            and (contains is None or contains in message.data)
        ]
        # 流式连接的消息数远超单次可读上限，保留最新的一段比保留最早的更有用。
        selected = matched[-limit:] if limit is not None else matched
        rendered = []
        for message in selected:
            item = {**message.public_dict(), "data": message.data}
            parsed = _parse_json(message.data)
            if parsed is not None:
                item["json"] = parsed
            rendered.append(item)

        summary = _sse_summary(messages)
        full = {
            "exchange_id": exchange.exchange_id,
            "url": exchange.url,
            "state": exchange.state,
            "messages": rendered,
            "returned_count": len(rendered),
            "matched_count": len(matched),
            **summary,
        }
        model = {
            "exchange_id": exchange.exchange_id,
            "url": redact_url(exchange.url),
            "state": exchange.state,
            "returned_count": len(rendered),
            "matched_count": len(matched),
            **summary,
            "note": "消息内容只返回给调用方进程，模型侧只提供事件名与字节统计",
        }
        return full, model

    # ------------------------------------------------------------------
    # 接口契约
    # ------------------------------------------------------------------

    async def analyze_api(
        self,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """把同一 endpoint 的多次交换归纳成可照着写代码的接口契约。"""

        group = self._endpoint_group(arguments)
        analysis = analyze_endpoint(group)
        analysis["related_exchange_ids"] = [item.exchange_id for item in group]
        return analysis, model_view(analysis)

    async def collect_pages(
        self,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """沿分页把整个接口的数据取全，并给出闭合证据。"""

        group = self._endpoint_group(arguments)
        analysis = analyze_endpoint(group)
        anchor = next(
            (item for item in reversed(group) if item.status and 200 <= item.status < 300),
            group[-1],
        )
        sample_body: str | None = None
        if (
            anchor.request_body is not None
            and anchor.request_body.text
            and not anchor.request_body.base64_encoded
        ):
            sample_body = anchor.request_body.text
        plan = build_plan(
            sample_url=anchor.url,
            analysis=analysis,
            overrides=arguments,
            sample_body=sample_body,
        )
        if plan.page_in == "body" and anchor.method in {"GET", "HEAD"}:
            # 浏览器不会给 GET/HEAD 带请求体，重放时会被丢掉，每页都会是同一页。
            raise ValueError(f"{anchor.method} 请求不能用请求体承载分页，请改用 page_in=query")
        max_pages = int(arguments.get("max_pages") or DEFAULT_MAX_PAGES)
        if not 1 <= max_pages <= MAX_PAGES:
            raise ValueError(f"max_pages 必须在 1 到 {MAX_PAGES} 之间")
        delay_ms = int(arguments.get("delay_ms") or 0)
        if not 0 <= delay_ms <= 10_000:
            raise ValueError("delay_ms 必须在 0 到 10000 之间")
        dedupe_key = arguments.get("dedupe_key")
        total_path = arguments.get("total_path")

        outcome = CollectionOutcome(plan=plan)
        seen: set[str] = set()
        cursor: str | None = None
        next_url: str | None = None
        exhausted_budget = True
        for index in range(max_pages):
            if index and delay_ms:
                await asyncio.sleep(delay_ms / 1000)
            if plan.cursor_source == "link" and index:
                # 下一页 URL 由服务端的 Link 响应头给出，本地不拼参数。
                if not next_url:
                    exhausted_budget = False
                    break
                url = next_url
            else:
                url = page_url(plan, anchor.url, index, cursor)
            attempt = PageAttempt(index=index, url=url)
            outcome.pages.append(attempt)
            replay_args: dict[str, Any] = {"exchange_id": anchor.exchange_id, "url": url}
            try:
                body = page_body(plan, sample_body, index, cursor)
            except ValueError as exc:
                attempt.error = str(exc)
                break
            if body is not None:
                replay_args["body"] = body
            try:
                full, _ = await self.replay(replay_args)
            except Exception as exc:
                attempt.error = f"{type(exc).__name__}: {exc}"
                break
            attempt.status = full.get("status")
            if not full.get("success") or not isinstance(attempt.status, int):
                attempt.error = str(full.get("error") or f"HTTP {attempt.status}")
                break
            if not 200 <= attempt.status < 300:
                attempt.error = f"服务端返回 {attempt.status}"
                break
            payload = full.get("json")
            if payload is None:
                attempt.error = "响应不是 JSON，无法按记录路径取数"
                break

            if outcome.declared_total is None:
                outcome.declared_total = extract_total(payload, total_path)
            records = extract_records(payload, plan.record_path)
            attempt.records = len(records)
            for record in records:
                fingerprint = record_fingerprint(record, dedupe_key)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                outcome.records.append(record)
                attempt.new_records += 1
            if len(outcome.records) > MAX_RECORDS:
                attempt.error = f"记录数超过 {MAX_RECORDS} 条上限"
                break

            if not records:
                exhausted_budget = False
                break
            # 参数名猜错时服务端通常照返第一页；零新增即停，否则会一直抓到页数上限
            # 还自认为成功。
            if index and attempt.new_records == 0:
                attempt.error = f"本页 {attempt.records} 条记录全部与前面重复，" + (
                    f"服务端可能忽略了分页参数 {plan.param}"
                    if plan.param
                    else "服务端给出的下一页与上一页内容相同"
                )
                break
            if plan.page_size is not None and len(records) < plan.page_size:
                exhausted_budget = False
                break
            if outcome.declared_total is not None and len(outcome.records) >= (
                outcome.declared_total
            ):
                exhausted_budget = False
                break
            if plan.strategy == "cursor":
                headers = full.get("headers")
                headers = headers if isinstance(headers, Mapping) else {}
                if plan.cursor_source == "link":
                    next_url = extract_next_link(headers)
                    exhausted = not next_url
                elif plan.cursor_source == "header":
                    cursor = extract_cursor_from_headers(headers, plan.cursor_header)
                    exhausted = not cursor
                else:
                    cursor = extract_cursor(payload, plan.cursor_field)
                    exhausted = not cursor
                if exhausted:
                    # 服务端不再给下一页游标，就是走到尽头了。
                    exhausted_budget = False
                    break

        decide_closure(outcome, exhausted_budget=exhausted_budget)
        evidence = outcome.evidence()
        full_view = {
            **evidence,
            "endpoint": analysis.get("endpoint", {}),
            "records": outcome.records,
        }
        # 记录就是业务数据本身；给模型等于开一条绕过采集完整性门逐条读数的旁路。
        model_view = {**evidence, "endpoint": analysis.get("endpoint", {})}
        model_view.pop("pages", None)
        return full_view, model_view

    def _endpoint_group(self, arguments: Mapping[str, Any]) -> list[NetworkExchange]:
        """定位目标 endpoint，并把同签名的其它交换一并带上作为归纳样本。"""

        identifier = _optional_text(arguments.get("exchange_id"), "exchange_id", 80)
        url_contains = _optional_text(arguments.get("url_contains"), "url_contains", 500)
        if identifier is not None:
            anchor = self._require_exchange(identifier)
        else:
            if url_contains is None:
                raise ValueError("必须提供 exchange_id 或 url_contains 之一")
            matches = self.log.select(url_contains=url_contains, limit=_MAX_LISTING)
            # 归纳要基于成功响应；失败请求的正文通常是错误页，推断不出真实结构。
            successful = [
                item for item in matches if item.status is not None and 200 <= item.status < 300
            ]
            candidates = successful or matches
            if not candidates:
                raise ValueError(f"没有匹配 {url_contains} 的流量交换")
            anchor = candidates[-1]
        signature = endpoint_signature(anchor)
        group = [
            item
            for item in self.log.select(limit=self.config.max_exchanges)
            if endpoint_signature(item) == signature
        ]
        return group or [anchor]

    # ------------------------------------------------------------------
    # 代码导出
    # ------------------------------------------------------------------

    async def export_code(
        self,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """把一次交换导出成可独立运行的调用代码。"""

        exchange = self._require_exchange(arguments.get("exchange_id"))
        target = _optional_text(arguments.get("target"), "target", 40) or "curl"
        include_secrets = _bool(arguments.get("include_secrets"), "include_secrets")
        body = exchange.request_body
        result = build_request_code(
            target=target,
            method=exchange.method,
            url=exchange.url,
            headers=exchange.request_headers,
            body=body.text if body is not None else None,
            body_is_binary=bool(body is not None and body.base64_encoded),
            include_secrets=include_secrets,
        )
        full = {
            "exchange_id": exchange.exchange_id,
            "method": exchange.method,
            "url": exchange.url,
            **result,
        }
        model = {
            "exchange_id": exchange.exchange_id,
            "target": result["target"],
            "language": result["language"],
            "body_kind": result["body_kind"],
            "placeholder_env_names": [item["env"] for item in result["placeholders"]],
            "code_chars": len(result["code"]),
            "note": "生成的代码只回给调用方进程，模型侧只提供目标语言与占位变量名",
        }
        return full, model

    # ------------------------------------------------------------------
    # HAR
    # ------------------------------------------------------------------

    async def export_har(
        self,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        collection_name = _optional_text(arguments.get("collection_name"), "collection_name", 100)
        if collection_name is None or _COLLECTION_NAME.fullmatch(collection_name) is None:
            raise ValueError("HAR 集合名称只能包含中英文、数字、下划线或连字符")
        include_bodies = _bool(arguments.get("include_bodies"), "include_bodies", default=True)
        selected = self.log.select(
            url_contains=_optional_text(arguments.get("url_contains"), "url_contains", 500) or "",
            methods=_string_list(arguments.get("methods"), "methods"),
            resource_types=_string_list(arguments.get("resource_types"), "resource_types"),
            status_min=_bounded_int(
                arguments.get("status_min"), "status_min", 100, 599, default=None
            ),
            status_max=_bounded_int(
                arguments.get("status_max"), "status_max", 100, 599, default=None
            ),
            only_failed=_bool(arguments.get("only_failed"), "only_failed"),
            limit=_bounded_int(
                arguments.get("limit"), "limit", 1, self.config.max_exchanges, default=_MAX_LISTING
            ),
        )
        if not selected:
            raise ValueError("当前过滤条件没有匹配到任何流量交换")
        document = build_har(selected, include_bodies=include_bodies)
        payload = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        path = await _write_private_async(
            self.artifact_root / "network-traffic",
            f"{collection_name}-{time.time_ns()}.har",
            payload,
        )
        summary = {
            "har_path": str(path),
            "entry_count": len(document["log"]["entries"]),
            "websocket_count": len(document["log"]["_websockets"]),
            "sse_count": sum(1 for item in selected if item.is_event_source),
            "byte_count": len(payload),
            "include_bodies": include_bodies,
        }
        return dict(summary), dict(summary)

    # ------------------------------------------------------------------
    # 重放
    # ------------------------------------------------------------------

    async def replay(self, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        source_id = arguments.get("exchange_id")
        source: dict[str, Any] | None = None
        if source_id is not None:
            exchange = self._require_exchange(source_id)
            source = {
                "exchange_id": exchange.exchange_id,
                "url": exchange.url,
                "method": exchange.method,
                "request_headers": dict(exchange.request_headers),
                "request_body_text": (
                    exchange.request_body.text if exchange.request_body is not None else None
                ),
            }
        request = build_replay_request(
            arguments,
            source=source,
            allowed_origins=self.allowed_origins,
        )
        context = self._session_source() if self._session_source is not None else None
        if context is None:
            raise ValueError("重放需要已打开的页面会话，请先导航到任务允许的页面")
        result: ReplayResult = await perform_replay(
            context.session,
            context.router,
            request,
            page_url=context.page_url,
            traffic=self.log,
            max_body_bytes=self.config.max_body_bytes,
        )
        full = result.full_dict()
        full["request"] = {
            "url": request.url,
            "method": request.method,
            "headers": dict(request.headers),
            "body": request.body,
        }
        if result.body is not None and result.body.text is not None:
            parsed = _parse_json(result.body.text)
            if parsed is not None:
                full["json"] = parsed
        model = result.model_dict()
        model["request"] = {
            "url": request.url,
            "method": request.method,
            "header_names": sorted(name for name, _ in request.headers),
            "body_bytes": len(request.body.encode("utf-8")) if request.body else 0,
        }
        return full, model

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _require_exchange(self, exchange_id: Any) -> NetworkExchange:
        identifier = _optional_text(exchange_id, "exchange_id", 80)
        if identifier is None:
            raise ValueError("必须提供 exchange_id")
        exchange = self.log.get(identifier)
        if exchange is None:
            raise ValueError(f"流量交换不存在或已被滚动淘汰：{identifier}")
        return exchange


async def _write_private_async(directory: Path, filename: str, payload: bytes) -> Path:
    def _write() -> Path:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        path = directory / filename
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
        return path

    return await asyncio.to_thread(_write)


def _parse_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def _bounded_int(
    value: Any,
    label: str,
    minimum: int,
    maximum: int,
    *,
    default: int | None,
) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _bool(value: Any, label: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{label} 必须是布尔值")
    return value


def _optional_text(value: Any, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空文本")
    result = value.strip()
    if len(result) > maximum or any(ord(character) < 32 for character in result):
        raise ValueError(f"{label} 超出长度或包含控制字符")
    return result


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError(f"{label} 必须是最多 20 项的数组")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{label} 包含无效项")
        items.append(item.strip())
    return tuple(items)

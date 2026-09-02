"""受管浏览器的完整流量日志：请求/响应头、时序、发起方、正文与 WebSocket 帧。

只记录当前受管浏览器真实发生的交换，不代理外部流量，也不凭空发起请求。
完整结果返回给调用方进程，模型侧一律走 `public_dict` 的有界脱敏视图。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.config import NetworkTrafficConfig
from witty_browser_auto.security.redaction import redact, redact_url

logger = logging.getLogger(__name__)

_WEBSOCKET_OPCODES = {
    0: "continuation",
    1: "text",
    2: "binary",
    8: "close",
    9: "ping",
    10: "pong",
}
_MAX_INITIATOR_FRAMES = 5
_MAX_HEADER_VALUE_PREVIEW = 200
_MAX_SAN_ENTRIES = 20
_SEARCH_SNIPPET_CONTEXT = 120

# 每个搜索范围对应按优先级扫描的字段序列；命中优先取靠前的字段作为现场。
_SEARCH_SCOPES: dict[str, tuple[str, ...]] = {
    "response_body": ("response_body",),
    "request_body": ("request_body",),
    "body": ("response_body", "request_body"),
    "headers": ("response_header", "request_header"),
    "websocket": ("websocket_frame",),
    "sse": ("sse_message",),
    "all": (
        "response_body",
        "request_body",
        "response_header",
        "request_header",
        "websocket_frame",
        "sse_message",
    ),
}


def _find_snippet(text: str, query: str, *, case_sensitive: bool) -> tuple[str, int]:
    """返回首个命中周围的片段与命中次数；未命中时次数为 0。"""

    if not query:
        return "", 0
    haystack = text if case_sensitive else text.casefold()
    needle = query if case_sensitive else query.casefold()
    index = haystack.find(needle)
    if index < 0:
        return "", 0
    count = haystack.count(needle)
    start = max(0, index - _SEARCH_SNIPPET_CONTEXT)
    end = min(len(text), index + len(query) + _SEARCH_SNIPPET_CONTEXT)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}", count


def _millis(value: Any) -> float | None:
    """CDP ResourceTiming 用 -1 表示该阶段不适用。"""

    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return round(float(value), 3)
    return None


def _span(start: Any, end: Any) -> float | None:
    first = _millis(start)
    last = _millis(end)
    if first is None or last is None or last < first:
        return None
    return round(last - first, 3)


@dataclass(frozen=True, slots=True)
class NetworkTiming:
    """按 HAR 语义分解的耗时，单位毫秒；无法采集的阶段为 None。"""

    blocked_ms: float | None = None
    dns_ms: float | None = None
    connect_ms: float | None = None
    ssl_ms: float | None = None
    send_ms: float | None = None
    wait_ms: float | None = None
    receive_ms: float | None = None
    total_ms: float | None = None

    @classmethod
    def from_cdp(cls, timing: Any, *, total_ms: float | None) -> NetworkTiming:
        if not isinstance(timing, Mapping):
            return cls(total_ms=total_ms)
        send_end = _millis(timing.get("sendEnd"))
        headers_end = _millis(timing.get("receiveHeadersEnd"))
        wait_ms = None
        if send_end is not None and headers_end is not None and headers_end >= send_end:
            wait_ms = round(headers_end - send_end, 3)
        receive_ms = None
        if total_ms is not None and headers_end is not None and total_ms >= headers_end:
            receive_ms = round(total_ms - headers_end, 3)
        blocked_candidates = [
            _millis(timing.get(key)) for key in ("dnsStart", "connectStart", "sendStart")
        ]
        blocked_ms = next((value for value in blocked_candidates if value is not None), None)
        return cls(
            blocked_ms=blocked_ms,
            dns_ms=_span(timing.get("dnsStart"), timing.get("dnsEnd")),
            connect_ms=_span(timing.get("connectStart"), timing.get("connectEnd")),
            ssl_ms=_span(timing.get("sslStart"), timing.get("sslEnd")),
            send_ms=_span(timing.get("sendStart"), timing.get("sendEnd")),
            wait_ms=wait_ms,
            receive_ms=receive_ms,
            total_ms=total_ms,
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "blocked_ms": self.blocked_ms,
            "dns_ms": self.dns_ms,
            "connect_ms": self.connect_ms,
            "ssl_ms": self.ssl_ms,
            "send_ms": self.send_ms,
            "wait_ms": self.wait_ms,
            "receive_ms": self.receive_ms,
            "total_ms": self.total_ms,
        }


@dataclass(frozen=True, slots=True)
class NetworkBody:
    """正文载荷；`text` 为 None 时由 `reason` 说明缺失原因。"""

    text: str | None = None
    byte_length: int = 0
    base64_encoded: bool = False
    truncated: bool = False
    reason: str = ""
    # 超过内存上限的响应落盘后，正文在这个私有文件里而不在 text 上。
    spill_path: str = ""

    @property
    def available(self) -> bool:
        return self.text is not None

    def raw_bytes(self) -> bytes:
        if self.text is None:
            return b""
        if self.base64_encoded:
            try:
                return base64.b64decode(self.text, validate=True)
            except (binascii.Error, ValueError):
                return b""
        return self.text.encode("utf-8")

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "available": self.available,
            "byte_length": self.byte_length,
            "base64_encoded": self.base64_encoded,
            "truncated": self.truncated,
            "reason": self.reason,
        }
        if self.spill_path:
            payload["spill_path"] = self.spill_path
        return payload


@dataclass(frozen=True, slots=True)
class WebSocketFrame:
    direction: str
    opcode: str
    payload: str
    byte_length: int
    truncated: bool
    timestamp: float

    def public_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "opcode": self.opcode,
            "byte_length": self.byte_length,
            "truncated": self.truncated,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    """一条 SSE 消息；`text/event-stream` 连接不关闭也能逐条记录。"""

    event: str
    data: str
    event_id: str
    byte_length: int
    truncated: bool
    timestamp: float

    def public_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "event_id": self.event_id,
            "byte_length": self.byte_length,
            "truncated": self.truncated,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class TrafficMatch:
    """正文/头/帧全文搜索命中；一次交换至多产出一条，记录首个命中现场。"""

    exchange: NetworkExchange
    part: str
    field_name: str
    match_count: int
    snippet: str


@dataclass(slots=True)
class NetworkExchange:
    """一次请求/响应交换；WebSocket 连接同样以一条交换承载其全部帧。"""

    exchange_id: str
    request_id: str
    session_id: str
    method: str = "GET"
    url: str = ""
    resource_type: str = "Other"
    frame_id: str = ""
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: NetworkBody | None = None
    has_post_data: bool = False
    initiator: dict[str, Any] = field(default_factory=dict)
    started_wall: float = 0.0
    started_mono: float = 0.0
    request_time: float | None = None
    status: int | None = None
    status_text: str = ""
    response_headers: dict[str, str] = field(default_factory=dict)
    mime_type: str = ""
    protocol: str = ""
    remote_address: str = ""
    remote_port: int | None = None
    from_disk_cache: bool = False
    from_service_worker: bool = False
    security_state: str = ""
    security_details: dict[str, Any] = field(default_factory=dict)
    timing: NetworkTiming = field(default_factory=NetworkTiming)
    timing_source: Any = None
    encoded_bytes: int = 0
    response_body: NetworkBody | None = None
    state: str = "pending"
    error_text: str = ""
    blocked_reason: str = ""
    cors_error: str = ""
    redirect_to: str = ""
    is_websocket: bool = False
    websocket_frames: list[WebSocketFrame] = field(default_factory=list)
    is_event_source: bool = False
    sse_messages: list[ServerSentEvent] = field(default_factory=list)
    replay_of: str | None = None
    finished_mono: float | None = None

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc

    @property
    def path(self) -> str:
        return urlsplit(self.url).path or "/"

    def full_dict(self) -> dict[str, Any]:
        """完整视图：给外部代码调用方，包含 Header 值与正文长度。"""

        payload: dict[str, Any] = {
            "exchange_id": self.exchange_id,
            "state": self.state,
            "method": self.method,
            "url": self.url,
            "host": self.host,
            "path": self.path,
            "resource_type": self.resource_type,
            "frame_id": self.frame_id,
            "status": self.status,
            "status_text": self.status_text,
            "mime_type": self.mime_type,
            "protocol": self.protocol,
            "remote_address": self.remote_address,
            "remote_port": self.remote_port,
            "from_disk_cache": self.from_disk_cache,
            "from_service_worker": self.from_service_worker,
            "security_state": self.security_state,
            "security_details": dict(self.security_details),
            "request_headers": dict(self.request_headers),
            "response_headers": dict(self.response_headers),
            "initiator": dict(self.initiator),
            "timing": self.timing.public_dict(),
            "encoded_bytes": self.encoded_bytes,
            "started_at": self.started_wall,
            "request_body": (
                self.request_body.public_dict() if self.request_body is not None else None
            ),
            "response_body": (
                self.response_body.public_dict() if self.response_body is not None else None
            ),
        }
        if self.redirect_to:
            payload["redirect_to"] = self.redirect_to
        if self.state == "failed":
            payload["error_text"] = self.error_text
            payload["blocked_reason"] = self.blocked_reason
            payload["cors_error"] = self.cors_error
        if self.replay_of is not None:
            payload["replay_of"] = self.replay_of
        if self.is_websocket:
            payload["websocket"] = {
                "frame_count": len(self.websocket_frames),
                "frames": [frame.public_dict() for frame in self.websocket_frames],
            }
        if self.is_event_source:
            payload["event_source"] = {
                "message_count": len(self.sse_messages),
                "messages": [message.public_dict() for message in self.sse_messages],
            }
        return payload

    def model_dict(self) -> dict[str, Any]:
        """有界脱敏视图：给智能体循环里的模型，不含 Header 值与正文。"""

        payload: dict[str, Any] = {
            "exchange_id": self.exchange_id,
            "state": self.state,
            "method": self.method,
            "url": redact_url(self.url),
            "resource_type": self.resource_type,
            "status": self.status,
            "mime_type": self.mime_type,
            "protocol": self.protocol,
            "duration_ms": self.timing.total_ms,
            "encoded_bytes": self.encoded_bytes,
            "request_header_names": sorted(self.request_headers),
            "response_header_names": sorted(self.response_headers),
            "response_body_bytes": (
                self.response_body.byte_length if self.response_body is not None else None
            ),
        }
        if self.state == "failed":
            payload["error_text"] = self.error_text[:200]
            payload["blocked_reason"] = self.blocked_reason
        if self.is_websocket:
            payload["websocket_frame_count"] = len(self.websocket_frames)
        if self.is_event_source:
            payload["sse_message_count"] = len(self.sse_messages)
        return payload


class NetworkTrafficLog:
    """跨会话共享的流量日志；每个页面会话的记录器都写入同一个实例。"""

    def __init__(
        self,
        config: NetworkTrafficConfig,
        *,
        body_spill_root: Path | None = None,
    ) -> None:
        self.config = config
        self.body_spill_root = body_spill_root
        self._exchanges: OrderedDict[str, NetworkExchange] = OrderedDict()
        self._by_request: dict[tuple[str, str], str] = {}
        self._sequence = 0
        self._body_bytes = 0
        self._spill_bytes = 0
        self._replay_markers: dict[str, str] = {}
        self._early_headers: dict[tuple[str, str], dict[str, str]] = {}

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------

    def on_request(self, event: CdpEvent) -> None:
        request = event.params.get("request")
        request_id = event.params.get("requestId")
        session_id = event.session_id
        if (
            not isinstance(request, Mapping)
            or not isinstance(request_id, str)
            or not isinstance(session_id, str)
        ):
            return
        redirect = event.params.get("redirectResponse")
        if isinstance(redirect, Mapping):
            previous = self._current(session_id, request_id)
            if previous is not None:
                self._apply_response(previous, redirect, resource_type=previous.resource_type)
                previous.state = "finished"
                previous.redirect_to = str(request.get("url", ""))
                previous.finished_mono = time.monotonic()
        exchange = self._new_exchange(session_id, request_id)
        exchange.method = str(request.get("method", "GET")).upper()
        exchange.url = str(request.get("url", ""))
        exchange.resource_type = str(event.params.get("type", "Other"))
        exchange.frame_id = str(event.params.get("frameId", ""))
        exchange.request_headers = _header_map(request.get("headers"))
        exchange.has_post_data = bool(request.get("hasPostData"))
        exchange.initiator = _initiator(event.params.get("initiator"))
        exchange.request_time = _seconds(event.params.get("timestamp"))
        exchange.started_wall = _seconds(event.params.get("wallTime")) or time.time()
        exchange.started_mono = time.monotonic()
        exchange.replay_of = self._replay_markers.pop(exchange.url, None)
        post_data = request.get("postData")
        if isinstance(post_data, str) and post_data:
            exchange.request_body = self._make_body(post_data.encode("utf-8"), base64_encoded=False)
        early = self._early_headers.pop((session_id, request_id), None)
        if early:
            exchange.request_headers.update(early)

    def on_request_extra_info(self, event: CdpEvent) -> None:
        """`requestWillBeSentExtraInfo` 携带浏览器实际发出的 Header，含 Cookie。

        该事件可能早于 `requestWillBeSent` 到达，此时先暂存，等交换建立后再合并。
        """

        headers = _header_map(event.params.get("headers"))
        if not headers:
            return
        exchange = self._from_event(event)
        if exchange is not None:
            exchange.request_headers.update(headers)
            return
        request_id = event.params.get("requestId")
        session_id = event.session_id
        if not isinstance(request_id, str) or not isinstance(session_id, str):
            return
        if len(self._early_headers) >= 512:
            self._early_headers.pop(next(iter(self._early_headers)))
        self._early_headers[(session_id, request_id)] = headers

    def on_response(self, event: CdpEvent) -> None:
        exchange = self._from_event(event)
        response = event.params.get("response")
        if exchange is None or not isinstance(response, Mapping):
            return
        self._apply_response(exchange, response, resource_type=str(event.params.get("type", "")))

    def on_response_extra_info(self, event: CdpEvent) -> None:
        """`responseReceivedExtraInfo` 携带原始响应头，含 Set-Cookie。"""

        exchange = self._from_event(event)
        if exchange is None:
            return
        headers = _header_map(event.params.get("headers"))
        if headers:
            exchange.response_headers.update(headers)

    async def on_finished(self, session: CdpTargetSession, event: CdpEvent) -> None:
        exchange = self._from_event(event)
        if exchange is None:
            return
        encoded = event.params.get("encodedDataLength")
        if isinstance(encoded, (int, float)) and not isinstance(encoded, bool):
            exchange.encoded_bytes = int(encoded)
        finished_at = _seconds(event.params.get("timestamp"))
        total_ms: float | None = None
        if finished_at is not None and exchange.request_time is not None:
            total_ms = round(max(finished_at - exchange.request_time, 0.0) * 1000, 3)
        exchange.timing = NetworkTiming.from_cdp(exchange.timing_source, total_ms=total_ms)
        exchange.state = "finished"
        exchange.finished_mono = time.monotonic()
        await self._collect_bodies(session, exchange)
        self._release_request_key(exchange)

    def on_failed(self, event: CdpEvent) -> None:
        exchange = self._from_event(event)
        if exchange is None:
            return
        exchange.state = "failed"
        exchange.error_text = str(event.params.get("errorText", ""))
        blocked = event.params.get("blockedReason")
        if isinstance(blocked, str):
            exchange.blocked_reason = blocked
        cors = event.params.get("corsErrorStatus")
        if isinstance(cors, Mapping) and cors.get("corsError"):
            exchange.cors_error = str(cors["corsError"])
        finished_at = _seconds(event.params.get("timestamp"))
        if finished_at is not None and exchange.request_time is not None:
            total = round(max(finished_at - exchange.request_time, 0.0) * 1000, 3)
            exchange.timing = NetworkTiming.from_cdp(exchange.timing_source, total_ms=total)
        exchange.finished_mono = time.monotonic()
        self._release_request_key(exchange)

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    def on_websocket_created(self, event: CdpEvent) -> None:
        request_id = event.params.get("requestId")
        session_id = event.session_id
        if not isinstance(request_id, str) or not isinstance(session_id, str):
            return
        exchange = self._new_exchange(session_id, request_id)
        exchange.is_websocket = True
        exchange.resource_type = "WebSocket"
        exchange.method = "GET"
        exchange.url = str(event.params.get("url", ""))
        exchange.initiator = _initiator(event.params.get("initiator"))
        exchange.started_wall = time.time()
        exchange.started_mono = time.monotonic()
        exchange.state = "open"

    def on_websocket_handshake_request(self, event: CdpEvent) -> None:
        exchange = self._from_event(event)
        request = event.params.get("request")
        if exchange is None or not isinstance(request, Mapping):
            return
        exchange.request_headers.update(_header_map(request.get("headers")))
        exchange.request_time = _seconds(event.params.get("timestamp"))
        wall = _seconds(event.params.get("wallTime"))
        if wall is not None:
            exchange.started_wall = wall

    def on_websocket_handshake_response(self, event: CdpEvent) -> None:
        exchange = self._from_event(event)
        response = event.params.get("response")
        if exchange is None or not isinstance(response, Mapping):
            return
        status = response.get("status")
        if isinstance(status, (int, float)) and not isinstance(status, bool):
            exchange.status = int(status)
        exchange.status_text = str(response.get("statusText", ""))
        exchange.response_headers.update(_header_map(response.get("headers")))
        exchange.request_headers.update(_header_map(response.get("requestHeaders")))
        exchange.protocol = "websocket"

    def on_websocket_frame(self, event: CdpEvent, *, direction: str) -> None:
        exchange = self._from_event(event)
        response = event.params.get("response")
        if exchange is None or not isinstance(response, Mapping):
            return
        if len(exchange.websocket_frames) >= self.config.max_websocket_frames:
            exchange.websocket_frames.pop(0)
        payload = str(response.get("payloadData", ""))
        limit = self.config.max_websocket_frame_bytes
        encoded = payload.encode("utf-8", errors="replace")
        truncated = len(encoded) > limit
        opcode = response.get("opcode")
        exchange.websocket_frames.append(
            WebSocketFrame(
                direction=direction,
                opcode=_WEBSOCKET_OPCODES.get(
                    int(opcode) if isinstance(opcode, (int, float)) else -1, "unknown"
                ),
                payload=encoded[:limit].decode("utf-8", errors="replace") if truncated else payload,
                byte_length=len(encoded),
                truncated=truncated,
                timestamp=_seconds(event.params.get("timestamp")) or time.time(),
            )
        )

    def on_websocket_closed(self, event: CdpEvent) -> None:
        exchange = self._from_event(event)
        if exchange is None:
            return
        exchange.state = "finished"
        exchange.finished_mono = time.monotonic()
        self._release_request_key(exchange)

    def on_websocket_error(self, event: CdpEvent) -> None:
        exchange = self._from_event(event)
        if exchange is None:
            return
        exchange.error_text = str(event.params.get("errorMessage", ""))

    # ------------------------------------------------------------------
    # Server-Sent Events
    # ------------------------------------------------------------------

    def on_event_source_message(self, event: CdpEvent) -> None:
        """`eventSourceMessageReceived` 逐条给出 SSE 消息。

        SSE 是长连接，`loadingFinished` 往往不触发，靠 `getResponseBody` 读不到内容；
        逐条记录消息是唯一可靠的采集方式。复用 WebSocket 的帧数量与单帧字节上限。
        """

        exchange = self._from_event(event)
        if exchange is None:
            return
        exchange.is_event_source = True
        if exchange.resource_type in ("", "Other"):
            exchange.resource_type = "EventSource"
        if len(exchange.sse_messages) >= self.config.max_websocket_frames:
            exchange.sse_messages.pop(0)
        data = str(event.params.get("data", ""))
        limit = self.config.max_websocket_frame_bytes
        encoded = data.encode("utf-8", errors="replace")
        truncated = len(encoded) > limit
        exchange.sse_messages.append(
            ServerSentEvent(
                event=str(event.params.get("eventName", "") or "message"),
                data=encoded[:limit].decode("utf-8", errors="replace") if truncated else data,
                event_id=str(event.params.get("eventId", "")),
                byte_length=len(encoded),
                truncated=truncated,
                timestamp=_seconds(event.params.get("timestamp")) or time.time(),
            )
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def register_replay_marker(self, url: str, source_exchange_id: str) -> None:
        """让重放发出的请求在流量日志里能追溯到来源交换。"""

        self._replay_markers[url] = source_exchange_id

    def get(self, exchange_id: str) -> NetworkExchange | None:
        return self._exchanges.get(exchange_id)

    def discard_session(self, session_id: str) -> None:
        for key in tuple(self._by_request):
            if key[0] == session_id:
                self._by_request.pop(key, None)
        for key in tuple(self._early_headers):
            if key[0] == session_id:
                self._early_headers.pop(key, None)

    def select(
        self,
        *,
        url_contains: str = "",
        methods: Sequence[str] = (),
        resource_types: Sequence[str] = (),
        status_min: int | None = None,
        status_max: int | None = None,
        only_failed: bool = False,
        limit: int = 50,
    ) -> tuple[NetworkExchange, ...]:
        needle = url_contains.casefold()
        method_set = {item.upper() for item in methods}
        type_set = {item.casefold() for item in resource_types}
        selected: list[NetworkExchange] = []
        for exchange in reversed(self._exchanges.values()):
            if needle and needle not in exchange.url.casefold():
                continue
            if method_set and exchange.method not in method_set:
                continue
            if type_set and exchange.resource_type.casefold() not in type_set:
                continue
            if only_failed and exchange.state != "failed":
                continue
            if status_min is not None and (exchange.status is None or exchange.status < status_min):
                continue
            if status_max is not None and (exchange.status is None or exchange.status > status_max):
                continue
            selected.append(exchange)
            if len(selected) >= limit:
                break
        selected.reverse()
        return tuple(selected)

    def search(
        self,
        *,
        query: str,
        scope: str = "body",
        case_sensitive: bool = False,
        url_contains: str = "",
        resource_types: Sequence[str] = (),
        limit: int = 50,
    ) -> tuple[TrafficMatch, ...]:
        """在已抓取的正文、Header 值、WebSocket 帧与 SSE 消息里按子串搜索。

        回答"页面上这个订单号/价格是哪个接口返回的""哪个请求带了这个 token"这类问题，
        正文本就已在内存里，无需逐条 read_network_body。每次交换至多命中一条，取最新的
        `limit` 条。
        """

        parts = _SEARCH_SCOPES.get(scope)
        if parts is None:
            raise ValueError(f"不支持的搜索范围：{scope}")
        needle_url = url_contains.casefold()
        type_set = {item.casefold() for item in resource_types}
        matches: list[TrafficMatch] = []
        for exchange in reversed(self._exchanges.values()):
            if needle_url and needle_url not in exchange.url.casefold():
                continue
            if type_set and exchange.resource_type.casefold() not in type_set:
                continue
            match = self._match_exchange(exchange, query, parts, case_sensitive=case_sensitive)
            if match is not None:
                matches.append(match)
                if len(matches) >= limit:
                    break
        matches.reverse()
        return tuple(matches)

    def _match_exchange(
        self,
        exchange: NetworkExchange,
        query: str,
        parts: tuple[str, ...],
        *,
        case_sensitive: bool,
    ) -> TrafficMatch | None:
        total = 0
        first: tuple[str, str, str] | None = None  # (part, field_name, snippet)
        for part in parts:
            for field_name, text in self._iter_searchable(exchange, part):
                snippet, count = _find_snippet(text, query, case_sensitive=case_sensitive)
                if count == 0:
                    continue
                total += count
                if first is None:
                    first = (part, field_name, snippet)
        if first is None:
            return None
        return TrafficMatch(
            exchange=exchange,
            part=first[0],
            field_name=first[1],
            match_count=total,
            snippet=first[2],
        )

    @staticmethod
    def _iter_searchable(exchange: NetworkExchange, part: str) -> Iterable[tuple[str, str]]:
        if part == "response_body":
            body = exchange.response_body
            if body is not None and body.text is not None and not body.base64_encoded:
                yield "", body.text
        elif part == "request_body":
            body = exchange.request_body
            if body is not None and body.text is not None and not body.base64_encoded:
                yield "", body.text
        elif part == "request_header":
            for name, value in exchange.request_headers.items():
                yield name, value
        elif part == "response_header":
            for name, value in exchange.response_headers.items():
                yield name, value
        elif part == "websocket_frame":
            for frame in exchange.websocket_frames:
                yield frame.direction, frame.payload
        elif part == "sse_message":
            for message in exchange.sse_messages:
                yield message.event, message.data

    def stats(self) -> dict[str, Any]:
        states: dict[str, int] = {}
        for exchange in self._exchanges.values():
            states[exchange.state] = states.get(exchange.state, 0) + 1
        return {
            "exchange_count": len(self._exchanges),
            "states": states,
            "buffered_body_bytes": self._body_bytes,
            "body_budget_bytes": self.config.max_total_body_bytes,
            "spilled_body_bytes": self._spill_bytes,
            "spill_budget_bytes": self.config.max_total_spill_bytes if self.spill_enabled else 0,
        }

    @property
    def spill_enabled(self) -> bool:
        return bool(self.config.spill_body_bytes) and self.body_spill_root is not None

    def ordered(self) -> tuple[NetworkExchange, ...]:
        return tuple(self._exchanges.values())

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _new_exchange(self, session_id: str, request_id: str) -> NetworkExchange:
        self._sequence += 1
        exchange_id = f"ex-{self._sequence:06d}"
        exchange = NetworkExchange(
            exchange_id=exchange_id,
            request_id=request_id,
            session_id=session_id,
        )
        while len(self._exchanges) >= self.config.max_exchanges:
            _, evicted = self._exchanges.popitem(last=False)
            self._forget_bodies(evicted)
        self._exchanges[exchange_id] = exchange
        self._by_request[(session_id, request_id)] = exchange_id
        return exchange

    def _current(self, session_id: str, request_id: str) -> NetworkExchange | None:
        exchange_id = self._by_request.get((session_id, request_id))
        return self._exchanges.get(exchange_id) if exchange_id else None

    def _from_event(self, event: CdpEvent) -> NetworkExchange | None:
        request_id = event.params.get("requestId")
        session_id = event.session_id
        if not isinstance(request_id, str) or not isinstance(session_id, str):
            return None
        return self._current(session_id, request_id)

    def _release_request_key(self, exchange: NetworkExchange) -> None:
        key = (exchange.session_id, exchange.request_id)
        if self._by_request.get(key) == exchange.exchange_id:
            self._by_request.pop(key, None)

    @staticmethod
    def _apply_response(
        exchange: NetworkExchange,
        response: Mapping[str, Any],
        *,
        resource_type: str,
    ) -> None:
        status = response.get("status")
        if isinstance(status, (int, float)) and not isinstance(status, bool):
            exchange.status = int(status)
        exchange.status_text = str(response.get("statusText", ""))
        headers = _header_map(response.get("headers"))
        if headers:
            exchange.response_headers.update(headers)
        exchange.mime_type = str(response.get("mimeType", ""))
        exchange.protocol = str(response.get("protocol", ""))
        exchange.remote_address = str(response.get("remoteIPAddress", ""))
        port = response.get("remotePort")
        if isinstance(port, (int, float)) and not isinstance(port, bool):
            exchange.remote_port = int(port)
        exchange.from_disk_cache = bool(response.get("fromDiskCache"))
        exchange.from_service_worker = bool(response.get("fromServiceWorker"))
        exchange.security_state = str(response.get("securityState", ""))
        details = _security_details(response.get("securityDetails"))
        if details:
            exchange.security_details = details
        if resource_type:
            exchange.resource_type = resource_type
        exchange.timing_source = response.get("timing")

    async def _collect_bodies(
        self,
        session: CdpTargetSession,
        exchange: NetworkExchange,
    ) -> None:
        if exchange.has_post_data and exchange.request_body is None:
            exchange.request_body = await self._fetch_request_body(session, exchange)
        if exchange.is_event_source:
            # SSE 内容已逐条记录在 sse_messages 里，整段流式正文读了也是拼接噪声。
            exchange.response_body = NetworkBody(
                reason="SSE 内容以消息形式记录，见 read_sse_messages"
            )
            return
        if exchange.resource_type not in self.config.body_resource_types:
            exchange.response_body = NetworkBody(reason="资源类型不在正文采集范围内")
            return
        if exchange.encoded_bytes > self.config.max_body_bytes:
            # 大导出接口的响应装不进内存，但丢掉就彻底读不到了；落盘保住可读性。
            exchange.response_body = await self._spill_response_body(session, exchange)
            return
        exchange.response_body = await self._fetch_response_body(session, exchange)
        self._account_body(exchange.response_body)

    async def _spill_response_body(
        self,
        session: CdpTargetSession,
        exchange: NetworkExchange,
    ) -> NetworkBody:
        """把超过内存上限的响应写进私有文件，返回只带路径与长度的正文描述。"""

        oversized = NetworkBody(
            byte_length=exchange.encoded_bytes,
            reason="响应超过单体正文上限",
        )
        limit = self.config.spill_body_bytes
        if not limit or self.body_spill_root is None:
            return oversized
        if exchange.encoded_bytes > limit:
            return NetworkBody(
                byte_length=exchange.encoded_bytes,
                reason="响应超过落盘单体上限",
            )
        if self._spill_bytes + exchange.encoded_bytes > self.config.max_total_spill_bytes:
            return NetworkBody(
                byte_length=exchange.encoded_bytes,
                reason="落盘全局预算已用尽",
            )
        try:
            result = await session.call(
                "Network.getResponseBody",
                {"requestId": exchange.request_id},
            )
        except Exception as exc:
            return NetworkBody(
                byte_length=exchange.encoded_bytes,
                reason=f"CDP 未能返回响应正文：{type(exc).__name__}",
            )
        raw = result.get("body")
        if not isinstance(raw, str):
            return oversized
        if result.get("base64Encoded") is True:
            try:
                payload = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                return NetworkBody(
                    byte_length=exchange.encoded_bytes,
                    reason="响应正文的 base64 编码无效",
                )
        else:
            payload = raw.encode("utf-8")
        try:
            path = await asyncio.to_thread(
                _write_private_body,
                self.body_spill_root,
                f"{exchange.exchange_id}.bin",
                payload,
            )
        except OSError as exc:
            return NetworkBody(
                byte_length=exchange.encoded_bytes,
                reason=f"响应正文落盘失败：{type(exc).__name__}",
            )
        self._spill_bytes += len(payload)
        return NetworkBody(
            byte_length=len(payload),
            reason="正文已落盘，见 spill_path",
            spill_path=str(path),
        )

    async def _fetch_response_body(
        self,
        session: CdpTargetSession,
        exchange: NetworkExchange,
    ) -> NetworkBody:
        try:
            result = await session.call(
                "Network.getResponseBody",
                {"requestId": exchange.request_id},
            )
        except Exception as exc:
            return NetworkBody(reason=f"CDP 未能返回响应正文：{type(exc).__name__}")
        raw = result.get("body")
        if not isinstance(raw, str):
            return NetworkBody(reason="CDP 返回的响应正文为空")
        return self._body_from_cdp(raw, base64_encoded=result.get("base64Encoded") is True)

    async def _fetch_request_body(
        self,
        session: CdpTargetSession,
        exchange: NetworkExchange,
    ) -> NetworkBody:
        try:
            result = await session.call(
                "Network.getRequestPostData",
                {"requestId": exchange.request_id},
            )
        except Exception as exc:
            return NetworkBody(reason=f"CDP 未能返回请求正文：{type(exc).__name__}")
        raw = result.get("postData")
        if not isinstance(raw, str):
            return NetworkBody(reason="CDP 返回的请求正文为空")
        return self._body_from_cdp(raw, base64_encoded=False)

    def _body_from_cdp(self, raw: str, *, base64_encoded: bool) -> NetworkBody:
        if base64_encoded:
            try:
                payload = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                return NetworkBody(reason="响应正文的 base64 编码无效")
        else:
            payload = raw.encode("utf-8")
        return self._make_body(payload, base64_encoded=base64_encoded)

    def _make_body(self, payload: bytes, *, base64_encoded: bool) -> NetworkBody:
        limit = self.config.max_body_bytes
        truncated = len(payload) > limit
        clipped = payload[:limit] if truncated else payload
        if base64_encoded:
            text = base64.b64encode(clipped).decode("ascii")
        else:
            text = clipped.decode("utf-8", errors="replace")
        return NetworkBody(
            text=text,
            byte_length=len(payload),
            base64_encoded=base64_encoded,
            truncated=truncated,
        )

    def _account_body(self, body: NetworkBody | None) -> None:
        if body is None or body.text is None:
            return
        self._body_bytes += len(body.text)
        if self._body_bytes <= self.config.max_total_body_bytes:
            return
        for exchange in self._exchanges.values():
            if self._body_bytes <= self.config.max_total_body_bytes:
                break
            if exchange.response_body is not None and exchange.response_body.text is not None:
                self._body_bytes -= len(exchange.response_body.text)
                exchange.response_body = NetworkBody(
                    byte_length=exchange.response_body.byte_length,
                    reason="正文已按全局字节预算释放",
                )

    def _forget_bodies(self, exchange: NetworkExchange) -> None:
        body = exchange.response_body
        if body is not None and body.text is not None:
            self._body_bytes = max(self._body_bytes - len(body.text), 0)


def _write_private_body(directory: Path, filename: str, payload: bytes) -> Path:
    """落盘正文只对当前用户可读；同名已存在时按序号避让，不覆盖既有证据。"""

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / filename
    suffix = 0
    while True:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            suffix += 1
            path = directory / f"{Path(filename).stem}-{suffix}{Path(filename).suffix}"
            continue
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
        return path


def _seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _header_map(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(name): str(item) for name, item in value.items()}
    if isinstance(value, list):
        return {
            str(item["name"]): str(item.get("value", ""))
            for item in value
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
    return {}


def _initiator(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    payload: dict[str, Any] = {"type": str(value.get("type", "other"))}
    if isinstance(value.get("url"), str):
        payload["url"] = value["url"]
    line = value.get("lineNumber")
    if isinstance(line, (int, float)) and not isinstance(line, bool):
        payload["line_number"] = int(line)
    stack = value.get("stack")
    if isinstance(stack, Mapping):
        frames = stack.get("callFrames")
        if isinstance(frames, list):
            payload["call_frames"] = [
                {
                    "function_name": str(frame.get("functionName", "")),
                    "url": str(frame.get("url", "")),
                    "line_number": int(frame.get("lineNumber", 0) or 0),
                }
                for frame in frames[:_MAX_INITIATOR_FRAMES]
                if isinstance(frame, Mapping)
            ]
    return payload


def _security_details(value: Any) -> dict[str, Any]:
    """归一化 TLS 与证书信息；排查证书过期、协议降级、签发方不符时要用。"""

    if not isinstance(value, Mapping):
        return {}
    payload: dict[str, Any] = {}
    for source, target in (
        ("protocol", "protocol"),
        ("keyExchange", "key_exchange"),
        ("keyExchangeGroup", "key_exchange_group"),
        ("cipher", "cipher"),
        ("mac", "mac"),
        ("subjectName", "subject_name"),
        ("issuer", "issuer"),
        ("certificateTransparencyCompliance", "certificate_transparency"),
        ("serverSignatureAlgorithm", "server_signature_algorithm"),
    ):
        item = value.get(source)
        if isinstance(item, str) and item:
            payload[target] = item
        elif isinstance(item, int) and not isinstance(item, bool):
            payload[target] = item
    for source, target in (("validFrom", "valid_from"), ("validTo", "valid_to")):
        item = value.get(source)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            payload[target] = float(item)
    names = value.get("sanList")
    if isinstance(names, list):
        payload["san_list"] = [str(name) for name in names[:_MAX_SAN_ENTRIES] if name]
        if len(names) > _MAX_SAN_ENTRIES:
            payload["san_truncated"] = True
    if isinstance(value.get("encryptedClientHello"), bool):
        payload["encrypted_client_hello"] = value["encryptedClientHello"]
    return payload


def redacted_records(exchanges: Iterable[NetworkExchange]) -> tuple[dict[str, Any], ...]:
    """给日志与路径事件使用的持久化视图，Header 值统一脱敏。"""

    return tuple(
        {
            "exchange_id": exchange.exchange_id,
            "method": exchange.method,
            "url": redact_url(exchange.url),
            "status": exchange.status,
            "resource_type": exchange.resource_type,
            "state": exchange.state,
            "duration_ms": exchange.timing.total_ms,
            "request_headers": redact(
                {
                    name: value[:_MAX_HEADER_VALUE_PREVIEW]
                    for name, value in exchange.request_headers.items()
                }
            ),
            "response_headers": redact(
                {
                    name: value[:_MAX_HEADER_VALUE_PREVIEW]
                    for name, value in exchange.response_headers.items()
                }
            ),
        }
        for exchange in exchanges
    )

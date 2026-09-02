"""把流量日志导出为 HAR 1.2，便于用现成抓包工具二次分析。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from witty_browser_auto.network.traffic import NetworkBody, NetworkExchange

_HAR_VERSION = "1.2"
_CREATOR_NAME = "witty-browser-auto"


def build_har(
    exchanges: Sequence[NetworkExchange],
    *,
    include_bodies: bool = True,
    creator_version: str = "1",
) -> dict[str, Any]:
    return {
        "log": {
            "version": _HAR_VERSION,
            "creator": {"name": _CREATOR_NAME, "version": creator_version},
            "pages": [],
            "entries": [
                _entry(exchange, include_bodies=include_bodies)
                for exchange in exchanges
                if not exchange.is_websocket
            ],
            "_websockets": [
                _websocket_entry(exchange, include_bodies=include_bodies)
                for exchange in exchanges
                if exchange.is_websocket
            ],
        }
    }


def _entry(exchange: NetworkExchange, *, include_bodies: bool) -> dict[str, Any]:
    timing = exchange.timing
    return {
        "startedDateTime": _iso(exchange.started_wall),
        "time": timing.total_ms if timing.total_ms is not None else -1,
        "request": {
            "method": exchange.method,
            "url": exchange.url,
            "httpVersion": exchange.protocol or "HTTP/1.1",
            "cookies": _cookies(exchange.request_headers.get("Cookie", "")),
            "headers": _headers(exchange.request_headers),
            "queryString": [
                {"name": name, "value": value}
                for name, value in parse_qsl(urlsplit(exchange.url).query, keep_blank_values=True)
            ],
            "headersSize": -1,
            "bodySize": exchange.request_body.byte_length if exchange.request_body else 0,
            **(
                {"postData": _post_data(exchange)}
                if include_bodies and exchange.request_body is not None
                else {}
            ),
        },
        "response": {
            "status": exchange.status if exchange.status is not None else 0,
            "statusText": exchange.status_text,
            "httpVersion": exchange.protocol or "HTTP/1.1",
            "cookies": [],
            "headers": _headers(exchange.response_headers),
            "content": _content(exchange.response_body, exchange.mime_type, include_bodies),
            "redirectURL": exchange.redirect_to,
            "headersSize": -1,
            "bodySize": exchange.encoded_bytes,
        },
        "cache": {},
        "timings": {
            "blocked": _har_time(timing.blocked_ms),
            "dns": _har_time(timing.dns_ms),
            "connect": _har_time(timing.connect_ms),
            "send": _har_time(timing.send_ms, default=0),
            "wait": _har_time(timing.wait_ms, default=0),
            "receive": _har_time(timing.receive_ms, default=0),
            "ssl": _har_time(timing.ssl_ms),
        },
        "serverIPAddress": exchange.remote_address,
        "_resourceType": exchange.resource_type,
        "_exchangeId": exchange.exchange_id,
        "_state": exchange.state,
        "_initiator": exchange.initiator,
        **({"_errorText": exchange.error_text} if exchange.error_text else {}),
        **({"_replayOf": exchange.replay_of} if exchange.replay_of else {}),
        **(
            {"_securityDetails": dict(exchange.security_details)}
            if exchange.security_details
            else {}
        ),
        # SSE 是普通 HTTP 请求，消息挂在自己的 entry 上而不是另起一张表。
        **(
            {"_serverSentEvents": _sse_messages(exchange, include_bodies=include_bodies)}
            if exchange.is_event_source
            else {}
        ),
    }


def _sse_messages(exchange: NetworkExchange, *, include_bodies: bool) -> list[dict[str, Any]]:
    return [
        {
            "event": message.event,
            "id": message.event_id,
            "time": message.timestamp,
            "byteLength": message.byte_length,
            "truncated": message.truncated,
            **({"data": message.data} if include_bodies else {}),
        }
        for message in exchange.sse_messages
    ]


def _websocket_entry(exchange: NetworkExchange, *, include_bodies: bool) -> dict[str, Any]:
    return {
        "_exchangeId": exchange.exchange_id,
        "startedDateTime": _iso(exchange.started_wall),
        "url": exchange.url,
        "status": exchange.status,
        "requestHeaders": _headers(exchange.request_headers),
        "responseHeaders": _headers(exchange.response_headers),
        "frames": [
            {
                "type": frame.direction,
                "opcode": frame.opcode,
                "time": frame.timestamp,
                "byteLength": frame.byte_length,
                "truncated": frame.truncated,
                **({"data": frame.payload} if include_bodies else {}),
            }
            for frame in exchange.websocket_frames
        ],
    }


def _post_data(exchange: NetworkExchange) -> dict[str, Any]:
    body = exchange.request_body
    assert body is not None
    mime = exchange.request_headers.get("Content-Type", "application/octet-stream")
    payload: dict[str, Any] = {"mimeType": mime, "text": body.text or ""}
    if mime.split(";", 1)[0].strip().casefold() == "application/x-www-form-urlencoded":
        payload["params"] = [
            {"name": name, "value": value}
            for name, value in parse_qsl(body.text or "", keep_blank_values=True)
        ]
    return payload


def _content(body: NetworkBody | None, mime_type: str, include_bodies: bool) -> dict[str, Any]:
    content: dict[str, Any] = {
        "size": body.byte_length if body is not None else 0,
        "mimeType": mime_type or "application/octet-stream",
    }
    if body is None or not include_bodies:
        return content
    if body.text is None:
        content["comment"] = body.reason
        return content
    content["text"] = body.text
    if body.base64_encoded:
        content["encoding"] = "base64"
    if body.truncated:
        content["comment"] = "正文按单体上限截断"
    return content


def _headers(headers: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": name, "value": value} for name, value in headers.items()]


def _cookies(raw: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for item in raw.split(";"):
        name, separator, value = item.partition("=")
        if separator and name.strip():
            entries.append({"name": name.strip(), "value": value.strip()})
    return entries


def _har_time(value: float | None, *, default: float = -1) -> float:
    return value if value is not None else default


def _iso(epoch: float) -> str:
    if epoch <= 0:
        return datetime.now(UTC).isoformat()
    return datetime.fromtimestamp(epoch, UTC).isoformat()

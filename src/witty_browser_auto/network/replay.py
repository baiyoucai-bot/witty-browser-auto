"""请求重放与编辑重发：复用当前浏览器会话，不自建 HTTP 客户端。

页面上下文只执行下面这段固定模板，参数以 JSON 传入；调用方与模型都不能提供
JavaScript。`fetch` 无法设置的受限 Header 由一次性 Fetch 拦截补齐。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.network.routing import CdpNetworkRouter, ReplayInterception
from witty_browser_auto.network.traffic import NetworkBody, NetworkTrafficLog

logger = logging.getLogger(__name__)

_MAX_HEADERS = 50
_MAX_HEADER_VALUE = 16_000
_MAX_BODY_CHARS = 2 * 1024 * 1024
_METHODS_WITHOUT_BODY = frozenset({"GET", "HEAD"})

# 固定重放模板：只接受结构化参数，返回 base64 正文以保证二进制安全。
_REPLAY_SCRIPT = """
async (spec) => {
  const init = {
    method: spec.method,
    credentials: 'include',
    cache: 'no-store',
    redirect: 'follow',
    headers: spec.headers,
  };
  if (spec.body !== null && spec.body !== undefined) {
    init.body = spec.body;
  }
  if (spec.referrer !== null && spec.referrer !== undefined) {
    init.referrer = spec.referrer;
  }
  const started = performance.now();
  let response;
  try {
    response = await fetch(spec.url, init);
  } catch (error) {
    return JSON.stringify({
      ok: false,
      error: String((error && error.message) || error),
      duration_ms: Math.round(performance.now() - started),
    });
  }
  const buffer = await response.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const step = 0x8000;
  for (let index = 0; index < bytes.length; index += step) {
    binary += String.fromCharCode.apply(null, bytes.subarray(index, index + step));
  }
  const headers = [];
  response.headers.forEach((value, name) => { headers.push([name, value]); });
  return JSON.stringify({
    ok: true,
    status: response.status,
    status_text: response.statusText,
    headers: headers,
    body_base64: btoa(binary),
    byte_length: bytes.length,
    duration_ms: Math.round(performance.now() - started),
    response_type: response.type,
    final_url: response.url,
  });
}
"""


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    """一次重放的完整描述；`source_exchange_id` 只用于回填流量日志的溯源关系。"""

    url: str
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    removed_headers: tuple[str, ...] = ()
    body: str | None = None
    referrer: str | None = None
    source_exchange_id: str | None = None

    @property
    def origin(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"


@dataclass(frozen=True, slots=True)
class ReplayResult:
    success: bool
    status: int | None = None
    status_text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: NetworkBody | None = None
    duration_ms: int | None = None
    final_url: str = ""
    response_type: str = ""
    error: str = ""
    source_exchange_id: str | None = None

    def full_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "status_text": self.status_text,
            "headers": dict(self.headers),
            "body": self.body.public_dict() if self.body is not None else None,
            "body_text": self.body.text if self.body is not None else None,
            "duration_ms": self.duration_ms,
            "final_url": self.final_url,
            "response_type": self.response_type,
            "error": self.error,
            "source_exchange_id": self.source_exchange_id,
        }

    def model_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "status_text": self.status_text,
            "response_header_names": sorted(self.headers),
            "body_bytes": self.body.byte_length if self.body is not None else None,
            "duration_ms": self.duration_ms,
            "response_type": self.response_type,
            "error": self.error[:200],
            "source_exchange_id": self.source_exchange_id,
        }


def build_replay_request(
    spec: Mapping[str, Any],
    *,
    source: Mapping[str, Any] | None,
    allowed_origins: Sequence[str],
) -> ReplayRequest:
    """把工具参数与来源交换合并成一次重放；来源字段可被逐项覆盖。"""

    base_url = str(source.get("url", "")) if source else ""
    base_method = str(source.get("method", "GET")) if source else "GET"
    base_headers: dict[str, str] = dict(source.get("request_headers", {})) if source else {}
    base_body = source.get("request_body_text") if source else None

    url = _text(spec.get("url"), "url", 4096) or base_url
    if not url:
        raise ValueError("重放必须提供 url 或可解析的来源交换")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("重放目标必须是 HTTP 或 HTTPS 绝对地址")
    origin = f"{parts.scheme}://{parts.netloc}"
    if allowed_origins and origin not in set(allowed_origins):
        raise ValueError(f"重放目标不在任务允许的 origin 内：{origin}")

    method = (_text(spec.get("method"), "method", 20) or base_method or "GET").upper()
    if not method.isalpha():
        raise ValueError("重放方法必须是纯字母的 HTTP 方法")

    removed = _header_names(spec.get("remove_headers"))
    headers = dict(base_headers)
    headers.update(_headers(spec.get("headers")))
    # 这些 Header 由浏览器按最终请求体与连接状态计算，覆盖会导致请求发不出去。
    dropped = {name.casefold() for name in removed} | {
        "content-length",
        "connection",
        "transfer-encoding",
    }
    headers = {name: value for name, value in headers.items() if name.casefold() not in dropped}
    if len(headers) > _MAX_HEADERS:
        raise ValueError(f"重放 Header 最多 {_MAX_HEADERS} 项")

    body = spec.get("body")
    if body is None:
        body = base_body
    if body is not None:
        if not isinstance(body, str):
            raise ValueError("重放请求体必须是文本；二进制请求体请改用路由改写")
        if len(body) > _MAX_BODY_CHARS:
            raise ValueError("重放请求体超过 2 MiB 上限")
    if method in _METHODS_WITHOUT_BODY:
        body = None

    return ReplayRequest(
        url=url,
        method=method,
        headers=tuple(headers.items()),
        removed_headers=removed,
        body=body,
        referrer=_text(spec.get("referrer"), "referrer", 4096),
        source_exchange_id=str(source["exchange_id"]) if source else None,
    )


async def perform_replay(
    session: CdpTargetSession,
    router: CdpNetworkRouter | None,
    request: ReplayRequest,
    *,
    page_url: str,
    traffic: NetworkTrafficLog | None = None,
    max_body_bytes: int = 2 * 1024 * 1024,
) -> ReplayResult:
    """在页面上下文发起重放；受限 Header 由一次性拦截补齐，结束后立即撤销。"""

    page_parts = urlsplit(page_url)
    page_origin = (
        f"{page_parts.scheme}://{page_parts.netloc}"
        if page_parts.scheme and page_parts.netloc
        else ""
    )
    interception = ReplayInterception(
        url=request.url,
        method=request.method,
        header_overrides=request.headers,
        removed_headers=request.removed_headers,
        body=None,
        page_origin=page_origin,
    )
    if traffic is not None and request.source_exchange_id:
        traffic.register_replay_marker(request.url, request.source_exchange_id)
    armed = False
    if router is not None:
        try:
            await router.arm_replay(interception)
            armed = True
        except Exception as exc:
            logger.warning(
                "重放拦截未能安装，将只使用页面可设置的 Header",
                extra={"reason": type(exc).__name__},
            )
    started = time.monotonic()
    try:
        payload = await _evaluate_replay(session, request)
    finally:
        if armed and router is not None:
            try:
                await router.disarm_replay()
            except Exception:
                logger.warning("撤销重放拦截失败，任务继续执行", exc_info=True)
    if payload is None:
        return ReplayResult(
            success=False,
            error="页面上下文没有返回可解析的重放结果",
            duration_ms=round((time.monotonic() - started) * 1000),
            source_exchange_id=request.source_exchange_id,
        )
    if not payload.get("ok"):
        return ReplayResult(
            success=False,
            error=str(payload.get("error", "重放请求被浏览器拒绝")),
            duration_ms=_int_or_none(payload.get("duration_ms")),
            source_exchange_id=request.source_exchange_id,
        )
    headers = {
        str(name): str(value) for name, value in payload.get("headers", []) if isinstance(name, str)
    }
    body_base64 = str(payload.get("body_base64", ""))
    byte_length = _int_or_none(payload.get("byte_length")) or 0
    body = _decode_replay_body(body_base64, byte_length, headers, max_body_bytes)
    return ReplayResult(
        success=True,
        status=_int_or_none(payload.get("status")),
        status_text=str(payload.get("status_text", "")),
        headers=headers,
        body=body,
        duration_ms=_int_or_none(payload.get("duration_ms")),
        final_url=str(payload.get("final_url", "")),
        response_type=str(payload.get("response_type", "")),
        source_exchange_id=request.source_exchange_id,
    )


async def _evaluate_replay(
    session: CdpTargetSession,
    request: ReplayRequest,
) -> dict[str, Any] | None:
    spec = {
        "url": request.url,
        "method": request.method,
        "headers": _script_safe_headers(request.headers),
        "body": request.body,
        "referrer": request.referrer,
    }
    expression = f"({_REPLAY_SCRIPT.strip()})({json.dumps(spec, ensure_ascii=False)})"
    result = await session.call(
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "userGesture": False,
        },
    )
    exception = result.get("exceptionDetails")
    if isinstance(exception, Mapping):
        text = str(exception.get("text", "重放模板执行失败"))
        return {"ok": False, "error": text}
    value = result.get("result", {}).get("value")
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _script_safe_headers(headers: Sequence[tuple[str, str]]) -> dict[str, str]:
    """页面 `fetch` 拒绝设置的 Header 交给拦截层，避免整个请求被浏览器丢弃。"""

    forbidden = {
        "accept-charset",
        "accept-encoding",
        "access-control-request-headers",
        "access-control-request-method",
        "connection",
        "content-length",
        "cookie",
        "cookie2",
        "date",
        "dnt",
        "expect",
        "host",
        "keep-alive",
        "origin",
        "referer",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "via",
    }
    return {
        name: value[:_MAX_HEADER_VALUE]
        for name, value in headers
        if name.casefold() not in forbidden and not name.casefold().startswith(("proxy-", "sec-"))
    }


def _decode_replay_body(
    body_base64: str,
    byte_length: int,
    headers: Mapping[str, str],
    max_body_bytes: int,
) -> NetworkBody:
    if byte_length > max_body_bytes:
        return NetworkBody(byte_length=byte_length, reason="重放响应超过单体正文上限")
    try:
        payload = base64.b64decode(body_base64, validate=True)
    except (binascii.Error, ValueError):
        return NetworkBody(byte_length=byte_length, reason="重放响应正文编码无效")
    content_type = next(
        (value for name, value in headers.items() if name.casefold() == "content-type"),
        "",
    )
    if _looks_binary(content_type):
        return NetworkBody(
            text=base64.b64encode(payload).decode("ascii"),
            byte_length=len(payload),
            base64_encoded=True,
        )
    return NetworkBody(text=payload.decode("utf-8", errors="replace"), byte_length=len(payload))


def _looks_binary(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip().casefold()
    if not media:
        return False
    if media.startswith(("text/", "application/json")) or media.endswith(("+json", "+xml")):
        return False
    return not media.startswith("application/x-www-form-urlencoded")


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None


def _text(value: Any, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"重放参数 {label} 必须是非空文本")
    result = value.strip()
    if len(result) > maximum or any(ord(character) < 32 for character in result):
        raise ValueError(f"重放参数 {label} 超出长度或包含控制字符")
    return result


def _headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("重放 headers 必须是对象")
    result: dict[str, str] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("重放 headers 包含无效 Header 名")
        if not isinstance(raw, str):
            raise ValueError(f"重放 Header {name} 的值必须是文本")
        if "\r" in raw or "\n" in raw:
            raise ValueError(f"重放 Header {name} 的值包含非法换行")
        if len(raw) > _MAX_HEADER_VALUE:
            raise ValueError(f"重放 Header {name} 的值超过长度上限")
        result[name.strip()] = raw
    return result


def _header_names(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_HEADERS:
        raise ValueError(f"remove_headers 必须是最多 {_MAX_HEADERS} 项的数组")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("remove_headers 包含无效 Header 名")
        names.append(item.strip())
    return tuple(names)

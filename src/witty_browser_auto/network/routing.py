"""基于 CDP Fetch 的任务级网络路由。"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.cdp.protocol import CdpEvent

logger = logging.getLogger(__name__)

_ACTIONS = frozenset({"block", "modify_request", "mock_response", "modify_response"})
_BROWSER_MANAGED_HEADERS = frozenset({"content-length"})
# Chrome 的 Fetch.continueRequest 会直接拒绝这些逐跳或连接相关的 Header。
_FETCH_UNSAFE_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "cookie2",
        "host",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "via",
    }
)
_FETCH_UNSAFE_PREFIXES = ("proxy-", "sec-")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_MAX_RULES = 8
_MAX_HEADER_VALUE_LENGTH = 16_000


@dataclass(frozen=True, slots=True)
class NetworkRouteRule:
    rule_id: str
    url_pattern: str
    action: str
    method: str | None = None
    request_headers: tuple[tuple[str, str], ...] = ()
    request_method: str | None = None
    request_body: str | None = None
    response_status: int | None = None
    response_headers: tuple[tuple[str, str], ...] = ()
    response_body: str | None = None

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        allowed_origins: Sequence[str],
        rule_id: str,
    ) -> NetworkRouteRule:
        pattern = _required_text(config, "url_pattern", 2048)
        _assert_allowed_pattern(pattern, allowed_origins)
        action = _required_text(config, "action", 30)
        if action not in _ACTIONS:
            raise ValueError(f"不支持的网络路由动作：{action}")
        method = config.get("method")
        if method is not None:
            method = _http_method(method, "method")
        request_method = config.get("request_method")
        if request_method is not None:
            request_method = _http_method(request_method, "request_method")
        request_headers = _headers(config.get("request_headers", {}), "request_headers")
        response_headers = _headers(config.get("response_headers", {}), "response_headers")
        request_body = _optional_text(config, "request_body", 16_000)
        response_body = _optional_text(config, "response_body", 16_000)
        status = config.get("response_status")
        if status is not None and (
            isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599
        ):
            raise ValueError("response_status 必须是 100 到 599 的整数")
        if action == "modify_request" and not any(
            (request_headers, request_method, request_body is not None)
        ):
            raise ValueError("modify_request 至少需要提供一个请求修改字段")
        if action in {"mock_response", "modify_response"} and not (
            response_body is not None or response_headers or status is not None
        ):
            raise ValueError(f"{action} 至少需要提供一个响应修改字段")
        return cls(
            rule_id=rule_id,
            url_pattern=pattern,
            action=action,
            method=method,
            request_headers=request_headers,
            request_method=request_method,
            request_body=request_body,
            response_status=status,
            response_headers=response_headers,
            response_body=response_body,
        )

    @property
    def response_stage(self) -> bool:
        return self.action == "modify_response"

    def matches(self, url: str, method: str, *, response_stage: bool) -> bool:
        return (
            self.response_stage == response_stage
            and (self.method is None or self.method == method.upper())
            and fnmatch.fnmatchcase(url, self.url_pattern)
        )

    def public_dict(self) -> dict[str, Any]:
        host_rewrite = any(name.casefold() == "host" for name, _ in self.request_headers)
        return {
            "rule_id": self.rule_id,
            "url_pattern": self.url_pattern,
            "action": self.action,
            "method": self.method,
            "request_header_names": [name for name, _ in self.request_headers],
            "request_url_host_rewrite": host_rewrite,
            "request_body_bytes": len((self.request_body or "").encode("utf-8")),
            "response_status": self.response_status,
            "response_header_names": [name for name, _ in self.response_headers],
            "response_body_bytes": len((self.response_body or "").encode("utf-8")),
        }


@dataclass(frozen=True, slots=True)
class ReplayInterception:
    """一次性重放拦截：补齐 `fetch` 无法设置的 Header，并放开响应头可见性。

    只按目标 URL 与方法匹配，不注入自定义请求头，避免跨源重放触发额外预检。
    """

    url: str
    method: str
    header_overrides: tuple[tuple[str, str], ...] = ()
    removed_headers: tuple[str, ...] = ()
    body: str | None = None
    page_origin: str = ""

    def matches(self, url: str, method: str) -> bool:
        return url == self.url and method.upper() == self.method.upper()

    @property
    def host_override(self) -> str | None:
        """`Host` 不能作为 Header 下发，只能改写 URL authority。"""

        return next(
            (value for name, value in self.header_overrides if name.casefold() == "host"),
            None,
        )

    def apply_request_headers(self, raw: Any) -> list[dict[str, str]]:
        merged = _merge_headers(raw, self.header_overrides)
        removed = {name.casefold() for name in self.removed_headers}
        return [
            item
            for item in merged
            if item["name"].casefold() not in removed and _is_fetch_safe_header(item["name"])
        ]

    def apply_response_headers(self, raw: Any) -> list[dict[str, str]]:
        """让页面上下文的 fetch 能读到全部响应头，跨源时补齐 CORS 允许项。"""

        overrides: list[tuple[str, str]] = [("access-control-expose-headers", "*")]
        if self.page_origin:
            existing = {
                str(item.get("name", "")).casefold()
                for item in (raw if isinstance(raw, list) else [])
            }
            if "access-control-allow-origin" not in existing:
                overrides.append(("access-control-allow-origin", self.page_origin))
                overrides.append(("access-control-allow-credentials", "true"))
        return _merge_headers(raw, tuple(overrides))


class CdpNetworkRouter:
    """让每个 Fetch.requestPaused 事件都有明确的终结命令。"""

    def __init__(self, session: CdpTargetSession, allowed_origins: Sequence[str]) -> None:
        self.session = session
        self.allowed_origins = tuple(allowed_origins)
        self._rules: tuple[NetworkRouteRule, ...] = ()
        self._replay: ReplayInterception | None = None
        self._enabled = False
        self._lock = asyncio.Lock()
        self._unsubscribe = session.connection.subscribe(
            "Fetch.requestPaused",
            self._on_paused,
            session_id=session.session_id,
        )

    async def set_rules(self, rules: Sequence[NetworkRouteRule]) -> None:
        if len(rules) > _MAX_RULES:
            raise ValueError(f"网络路由最多保留 {_MAX_RULES} 条规则")
        async with self._lock:
            self._rules = tuple(rules)
            await self._apply_patterns()

    async def arm_replay(self, interception: ReplayInterception) -> None:
        async with self._lock:
            self._replay = interception
            await self._apply_patterns()

    async def disarm_replay(self) -> None:
        async with self._lock:
            self._replay = None
            await self._apply_patterns()

    async def _apply_patterns(self) -> None:
        """`Fetch.enable` 是整会话生效的，任务规则与重放拦截必须一起重算。"""

        if self._enabled:
            await self.session.call("Fetch.disable")
            self._enabled = False
        patterns = [
            {
                "urlPattern": rule.url_pattern,
                "requestStage": "Response" if rule.response_stage else "Request",
            }
            for rule in self._rules
        ]
        if self._replay is not None:
            patterns.append({"urlPattern": self._replay.url, "requestStage": "Request"})
            patterns.append({"urlPattern": self._replay.url, "requestStage": "Response"})
        if not patterns:
            return
        await self.session.call("Fetch.enable", {"patterns": patterns})
        self._enabled = True

    async def close(self) -> None:
        self._unsubscribe()
        async with self._lock:
            if self._enabled:
                try:
                    await self.session.call("Fetch.disable")
                except Exception:
                    logger.debug("关闭 Fetch 路由时页面已断开", exc_info=True)
                self._enabled = False
            self._rules = ()
            self._replay = None

    async def _on_paused(self, event: CdpEvent) -> None:
        params = event.params
        request = params.get("request")
        request_id = params.get("requestId")
        if not isinstance(request, dict) or not isinstance(request_id, str):
            return
        url = str(request.get("url", ""))
        method = str(request.get("method", "GET")).upper()
        response_stage = isinstance(params.get("responseStatusCode"), int) or (
            "responseErrorReason" in params
        )
        replay = self._replay
        if replay is not None and replay.matches(url, method):
            await self._handle_replay(request_id, params, request, replay, response_stage)
            return
        rule = next(
            (
                item
                for item in self._rules
                if item.matches(url, method, response_stage=response_stage)
            ),
            None,
        )
        try:
            if rule is None:
                await self.session.call("Fetch.continueRequest", {"requestId": request_id})
            elif rule.action == "block":
                await self.session.call(
                    "Fetch.failRequest",
                    {"requestId": request_id, "errorReason": "BlockedByClient"},
                )
            elif rule.action == "modify_request":
                await self._continue_modified_request(request_id, request, rule)
            elif rule.action == "mock_response":
                await self._fulfill_mock(request_id, rule)
            else:
                await self._fulfill_modified_response(request_id, params, rule)
        except Exception:
            logger.exception(
                "网络路由处理失败，尝试放行当前请求",
                extra={"rule_id": rule.rule_id if rule else "none"},
            )
            try:
                await self.session.call("Fetch.continueRequest", {"requestId": request_id})
            except Exception:
                logger.exception(
                    "网络路由兜底放行失败", extra={"rule_id": rule.rule_id if rule else "none"}
                )

    async def _handle_replay(
        self,
        request_id: str,
        params: Mapping[str, Any],
        request: Mapping[str, Any],
        replay: ReplayInterception,
        response_stage: bool,
    ) -> None:
        try:
            if response_stage:
                # continueResponse 要求状态码与响应头成对提供，只给其一会被直接拒绝。
                await self.session.call(
                    "Fetch.continueResponse",
                    {
                        "requestId": request_id,
                        "responseCode": int(params.get("responseStatusCode") or 200),
                        "responseHeaders": replay.apply_response_headers(
                            params.get("responseHeaders")
                        ),
                    },
                )
                return
            continue_params: dict[str, Any] = {
                "requestId": request_id,
                "headers": replay.apply_request_headers(request.get("headers", {})),
            }
            host = replay.host_override
            if host is not None:
                continue_params["url"] = _url_with_authority(str(request.get("url", "")), host)
            if replay.body is not None:
                continue_params["postData"] = base64.b64encode(replay.body.encode("utf-8")).decode(
                    "ascii"
                )
            await self.session.call("Fetch.continueRequest", continue_params)
        except Exception:
            logger.exception("重放拦截处理失败，放行原始请求")
            try:
                await self.session.call(
                    "Fetch.continueResponse" if response_stage else "Fetch.continueRequest",
                    {"requestId": request_id},
                )
            except Exception:
                logger.exception("重放拦截兜底放行失败")

    async def _continue_modified_request(
        self,
        request_id: str,
        request: Mapping[str, Any],
        rule: NetworkRouteRule,
    ) -> None:
        host = next(
            (value for name, value in rule.request_headers if name.casefold() == "host"),
            None,
        )
        header_overrides = tuple(
            (name, value) for name, value in rule.request_headers if name.casefold() != "host"
        )
        headers = _merge_headers(request.get("headers", {}), header_overrides)
        params: dict[str, Any] = {"requestId": request_id, "headers": headers}
        if host is not None:
            params["url"] = _url_with_authority(str(request.get("url", "")), host)
        if rule.request_method is not None:
            params["method"] = rule.request_method
        if rule.request_body is not None:
            params["postData"] = base64.b64encode(rule.request_body.encode("utf-8")).decode("ascii")
        await self.session.call("Fetch.continueRequest", params)

    async def _fulfill_mock(self, request_id: str, rule: NetworkRouteRule) -> None:
        await self.session.call("Fetch.fulfillRequest", _response_params(request_id, rule))

    async def _fulfill_modified_response(
        self,
        request_id: str,
        params: Mapping[str, Any],
        rule: NetworkRouteRule,
    ) -> None:
        if rule.response_body is None:
            original = await self.session.call("Fetch.getResponseBody", {"requestId": request_id})
            raw_body = str(original.get("body", ""))
            encoded_body = (
                raw_body
                if original.get("base64Encoded") is True
                else base64.b64encode(raw_body.encode("utf-8")).decode("ascii")
            )
        else:
            encoded_body = base64.b64encode(rule.response_body.encode("utf-8")).decode("ascii")
        headers = _merge_headers(params.get("responseHeaders", []), rule.response_headers)
        headers = [item for item in headers if item["name"].casefold() != "content-length"]
        await self.session.call(
            "Fetch.fulfillRequest",
            {
                "requestId": request_id,
                "responseCode": (
                    rule.response_status
                    if rule.response_status is not None
                    else int(params.get("responseStatusCode") or 200)
                ),
                "responseHeaders": headers,
                "body": encoded_body,
            },
        )


def _response_params(request_id: str, rule: NetworkRouteRule) -> dict[str, Any]:
    return {
        "requestId": request_id,
        "responseCode": rule.response_status or 200,
        "responseHeaders": [
            {"name": name, "value": value} for name, value in rule.response_headers
        ],
        "body": base64.b64encode((rule.response_body or "").encode("utf-8")).decode("ascii"),
    }


def _is_fetch_safe_header(name: str) -> bool:
    normalized = name.casefold()
    return normalized not in _FETCH_UNSAFE_HEADERS and not normalized.startswith(
        _FETCH_UNSAFE_PREFIXES
    )


def _merge_headers(raw: Any, overrides: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, tuple[str, str]] = {}
    if isinstance(raw, dict):
        merged.update(
            {str(name).casefold(): (str(name), str(value)) for name, value in raw.items()}
        )
    elif isinstance(raw, list):
        merged.update(
            {
                str(item.get("name")).casefold(): (
                    str(item.get("name")),
                    str(item.get("value", "")),
                )
                for item in raw
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
        )
    for name, value in overrides:
        merged[name.casefold()] = (name, value)
    return [{"name": name, "value": value} for name, value in merged.values()]


def _assert_allowed_pattern(pattern: str, allowed_origins: Sequence[str]) -> None:
    parsed = urlsplit(pattern.replace("*", "route-placeholder"))
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    if parsed.scheme not in {"http", "https"} or not origin or origin not in allowed_origins:
        raise ValueError("网络路由只能匹配当前任务允许的 HTTP/HTTPS origin")
    if "*" in parsed.netloc or "?" in parsed.netloc:
        raise ValueError("网络路由不能在 host 中使用通配符")


def _headers(value: Any, key: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or len(value) > 30:
        raise ValueError(f"{key} 必须是最多 30 项的对象")
    result: list[tuple[str, str]] = []
    for name, raw in value.items():
        header_name = _required_text({"value": str(name)}, "value", 100)
        if _HEADER_NAME.fullmatch(header_name) is None:
            raise ValueError(f"Header 名称格式无效：{header_name}")
        if header_name.casefold() in _BROWSER_MANAGED_HEADERS:
            raise ValueError(f"不允许修改浏览器管理的 Header：{header_name}")
        header_value = _required_text(
            {"value": str(raw)},
            "value",
            _MAX_HEADER_VALUE_LENGTH,
        )
        result.append((header_name, header_value))
    return tuple(result)


def _http_method(value: Any, key: str) -> str:
    method = _required_text({"value": str(value)}, "value", 20).upper()
    if not method.isalpha():
        raise ValueError(f"{key} 不是有效 HTTP 方法")
    return method


def _url_with_authority(url: str, authority: str) -> str:
    original = urlsplit(url)
    if original.scheme not in {"http", "https"} or not original.netloc:
        raise ValueError("Host 重写只支持 HTTP/HTTPS 请求")
    if any(character in authority for character in "/?#@"):
        raise ValueError("Host 重写值只能包含主机名和可选端口")
    parsed_authority = urlsplit(f"//{authority}")
    try:
        port = parsed_authority.port
    except ValueError as exc:
        raise ValueError("Host 重写端口无效") from exc
    if not parsed_authority.hostname or parsed_authority.username or parsed_authority.password:
        raise ValueError("Host 重写值必须是有效主机名和可选端口")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Host 重写端口无效")
    return urlunsplit(
        (
            original.scheme,
            authority,
            original.path,
            original.query,
            "",
        )
    )


def _optional_text(config: Mapping[str, Any], key: str, maximum: int) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    return _required_text({"value": str(value)}, "value", maximum)


def _required_text(config: Mapping[str, Any], key: str, maximum: int) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 不能为空")
    result = value.strip()
    if len(result) > maximum or any(ord(character) < 32 for character in result):
        raise ValueError(f"{key} 超出长度或包含控制字符")
    return result

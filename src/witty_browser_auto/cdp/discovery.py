"""远程调试端点发现与安全检查。"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from witty_browser_auto.domain.errors import CdpConnectionError, PolicyViolationError


@dataclass(frozen=True, slots=True)
class DevToolsEndpoint:
    websocket_url: str
    browser: str
    protocol_version: str
    user_agent: str


def ensure_loopback_endpoint(endpoint: str, *, allow_remote: bool = False) -> None:
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https", "ws", "wss"} or not parts.hostname:
        raise PolicyViolationError("CDP 地址格式无效", context={"endpoint": endpoint})
    if parts.username or parts.password:
        raise PolicyViolationError("CDP 地址不得包含明文账号或密码")
    if allow_remote:
        return
    hostname = parts.hostname.lower()
    if hostname == "localhost":
        return
    try:
        if ipaddress.ip_address(hostname).is_loopback:
            return
    except ValueError:
        pass
    raise PolicyViolationError(
        "默认只允许连接本机回环 CDP 地址",
        context={"endpoint": endpoint},
    )


async def discover_devtools_endpoint(
    endpoint: str,
    *,
    http_session: aiohttp.ClientSession,
    allow_remote: bool = False,
    timeout_seconds: float = 10.0,
) -> DevToolsEndpoint:
    ensure_loopback_endpoint(endpoint, allow_remote=allow_remote)
    if endpoint.startswith(("ws://", "wss://")):
        return DevToolsEndpoint(endpoint, "", "", "")

    version_url = f"{endpoint.rstrip('/')}/json/version"
    try:
        async with http_session.get(
            version_url,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            response.raise_for_status()
            payload: Any = await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise CdpConnectionError(
            "无法读取浏览器远程调试信息",
            context={"endpoint": endpoint},
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("webSocketDebuggerUrl"), str):
        raise CdpConnectionError("远程调试信息缺少 WebSocket 地址")
    websocket_url = payload["webSocketDebuggerUrl"]
    ensure_loopback_endpoint(websocket_url, allow_remote=allow_remote)
    return DevToolsEndpoint(
        websocket_url=websocket_url,
        browser=str(payload.get("Browser", "")),
        protocol_version=str(payload.get("Protocol-Version", "")),
        user_agent=str(payload.get("User-Agent", "")),
    )

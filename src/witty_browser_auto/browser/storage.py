"""受控 Cookie 与 Web Storage 读写：纯 CDP 协议，不依赖页面可见或前台焦点。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from witty_browser_auto.browser.frames import FrameHandle
from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.domain.errors import PolicyViolationError
from witty_browser_auto.memory.url import normalize_url

StorageKind = Literal["local", "session"]

_MAX_COOKIES = 200
_MAX_COOKIE_NAME_LENGTH = 256
_MAX_COOKIE_VALUE_LENGTH = 4096
_MAX_STORAGE_KEYS = 50
_MAX_STORAGE_KEY_LENGTH = 256
_MAX_STORAGE_VALUE_LENGTH = 65_536

_READ_WEB_STORAGE_SCRIPT = """
function(kind, key, listOnly) {
  const store = kind === 'session' ? sessionStorage : localStorage;
  if (listOnly || !key) {
    const keys = Object.keys(store);
    return {mode: 'keys', keys: keys.slice(0, 50), truncated: keys.length > 50};
  }
  const value = store.getItem(key);
  return {mode: 'value', key, value, found: value !== null};
}
"""

_WRITE_WEB_STORAGE_SCRIPT = """
function(kind, key, value, remove) {
  if (!key || typeof key !== 'string') {
    return {ok: false, reason: 'bad_key'};
  }
  const store = kind === 'session' ? sessionStorage : localStorage;
  if (remove) {
    store.removeItem(key);
    return {ok: true, action: 'removed'};
  }
  if (value === null || value === undefined) {
    return {ok: false, reason: 'missing_value'};
  }
  store.setItem(key, String(value));
  return {ok: true, action: 'set'};
}
"""


def allowed_origins_for_url(url: str, allowed_origins: Sequence[str]) -> None:
    """拒绝不在任务授权 origin 内的 URL。"""

    origin = normalize_url(url).origin
    if allowed_origins and origin not in set(allowed_origins):
        raise PolicyViolationError(f"URL 不在任务授权范围内：{origin}")


def _normalize_storage_kind(kind: str) -> StorageKind:
    if kind not in {"local", "session"}:
        raise ValueError("storage_kind 必须是 local 或 session")
    return kind  # type: ignore[return-value]


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit]


async def read_cookies(
    session: CdpTargetSession,
    url: str,
    *,
    names: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """读取指定 URL 可见的 Cookie；不触碰 Page.bringToFront。"""

    result = await session.call("Network.getCookies", {"urls": [url]})
    cookies = result.get("cookies")
    if not isinstance(cookies, list):
        return []
    name_filter = {item for item in names} if names else None
    normalized: list[dict[str, Any]] = []
    for item in cookies[:_MAX_COOKIES]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        if name_filter is not None and name not in name_filter:
            continue
        normalized.append(
            {
                "name": _bounded_text(name, _MAX_COOKIE_NAME_LENGTH),
                "value": _bounded_text(str(item.get("value", "")), _MAX_COOKIE_VALUE_LENGTH),
                "domain": _bounded_text(str(item.get("domain", "")), 253),
                "path": _bounded_text(str(item.get("path", "/")), 1024),
                "expires": item.get("expires"),
                "httpOnly": bool(item.get("httpOnly")),
                "secure": bool(item.get("secure")),
                "session": bool(item.get("session")),
                "sameSite": item.get("sameSite"),
            }
        )
    return normalized


async def set_cookie(
    session: CdpTargetSession,
    *,
    name: str,
    value: str,
    url: str,
    path: str = "/",
    domain: str | None = None,
    http_only: bool = False,
    secure: bool = False,
    expires: float | None = None,
) -> dict[str, Any]:
    """通过 Network.setCookie 写入 Cookie。"""

    if not name.strip():
        raise ValueError("Cookie 名称不能为空")
    if len(name) > _MAX_COOKIE_NAME_LENGTH:
        raise ValueError(f"Cookie 名称超过 {_MAX_COOKIE_NAME_LENGTH} 字符上限")
    if len(value) > _MAX_COOKIE_VALUE_LENGTH:
        raise ValueError(f"Cookie 值超过 {_MAX_COOKIE_VALUE_LENGTH} 字符上限")
    params: dict[str, Any] = {
        "name": name,
        "value": value,
        "url": url,
        "path": path or "/",
        "httpOnly": http_only,
        "secure": secure,
    }
    if domain:
        params["domain"] = domain
    if expires is not None:
        params["expires"] = expires
    await session.call("Network.setCookie", params)
    return {
        "name": name,
        "url": url,
        "path": params["path"],
        "domain": domain,
        "httpOnly": http_only,
        "secure": secure,
        "expires": expires,
    }


async def read_web_storage(
    frame: FrameHandle,
    *,
    storage_kind: StorageKind,
    key: str | None = None,
) -> dict[str, Any]:
    """在指定帧 document 上读取 localStorage 或 sessionStorage。"""

    kind = _normalize_storage_kind(storage_kind)
    if key is not None:
        if not key.strip():
            raise ValueError("storage key 不能为空")
        if len(key) > _MAX_STORAGE_KEY_LENGTH:
            raise ValueError(f"storage key 超过 {_MAX_STORAGE_KEY_LENGTH} 字符上限")
    result = await frame.call_on_document(
        _READ_WEB_STORAGE_SCRIPT,
        [
            {"value": kind},
            {"value": key} if key is not None else {"value": None},
            {"value": key is None},
        ],
    )
    payload = result.get("result", {}).get("value")
    if not isinstance(payload, dict):
        raise RuntimeError("浏览器没有返回 Web Storage 读取结果")
    mode = payload.get("mode")
    if mode == "keys":
        keys = payload.get("keys")
        if not isinstance(keys, list):
            keys = []
        return {
            "storage_kind": kind,
            "mode": "keys",
            "keys": [str(item) for item in keys if isinstance(item, str)],
            "truncated": bool(payload.get("truncated")),
        }
    if mode == "value":
        value = payload.get("value")
        if value is not None and not isinstance(value, str):
            value = str(value)
        if isinstance(value, str) and len(value) > _MAX_STORAGE_VALUE_LENGTH:
            value = value[:_MAX_STORAGE_VALUE_LENGTH]
        return {
            "storage_kind": kind,
            "mode": "value",
            "key": key,
            "value": value,
            "found": bool(payload.get("found")),
        }
    raise RuntimeError("Web Storage 读取结果格式未知")


async def write_web_storage(
    frame: FrameHandle,
    *,
    storage_kind: StorageKind,
    key: str,
    value: str | None = None,
    remove: bool = False,
) -> dict[str, Any]:
    """在指定帧 document 上写入或删除 Web Storage 项。"""

    kind = _normalize_storage_kind(storage_kind)
    if not key.strip():
        raise ValueError("storage key 不能为空")
    if len(key) > _MAX_STORAGE_KEY_LENGTH:
        raise ValueError(f"storage key 超过 {_MAX_STORAGE_KEY_LENGTH} 字符上限")
    if not remove:
        if value is None:
            raise ValueError("写入 Web Storage 必须提供 value 或 value_input_key")
        if len(value) > _MAX_STORAGE_VALUE_LENGTH:
            raise ValueError(f"storage value 超过 {_MAX_STORAGE_VALUE_LENGTH} 字符上限")
    result = await frame.call_on_document(
        _WRITE_WEB_STORAGE_SCRIPT,
        [
            {"value": kind},
            {"value": key},
            {"value": value},
            {"value": remove},
        ],
    )
    payload = result.get("result", {}).get("value")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        reason = payload.get("reason") if isinstance(payload, dict) else "unknown"
        raise RuntimeError(f"Web Storage 写入失败：{reason}")
    return {
        "storage_kind": kind,
        "key": key,
        "action": payload.get("action", "set"),
    }

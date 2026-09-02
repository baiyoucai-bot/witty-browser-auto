"""会话态整体存取：一次导出/导入 Cookie 与 Web Storage。

逐项读写已经由 `storage.py` 提供，但"跳过重复登录"需要的是整体快照——登录态往往同时
落在若干个 Cookie 和 localStorage 项里，逐个搬运既漏又慢。

快照结构对齐 Playwright 的 `storageState`，即 `cookies` 加 `origins[].localStorage`，
因此这里导出的文件可以直接喂给 Playwright，反之亦然。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MAX_COOKIES = 200
MAX_ORIGINS = 20
MAX_STORAGE_ENTRIES = 200
MAX_VALUE_LENGTH = 65_536
MAX_STATE_BYTES = 4_000_000

# 一次取回整个 storage，逐键读取在几十项时会变成几十次往返。
_DUMP_SCRIPT = """
function(limit, maxValue) {
  const dump = (store) => {
    const entries = [];
    for (let index = 0; index < store.length && entries.length < limit; index += 1) {
      const name = store.key(index);
      if (name === null) continue;
      const value = store.getItem(name);
      if (value === null) continue;
      entries.push({name, value: value.length > maxValue ? value.slice(0, maxValue) : value});
    }
    return entries;
  };
  return {
    origin: location.origin,
    localStorage: dump(localStorage),
    sessionStorage: dump(sessionStorage),
  };
}
"""

_RESTORE_SCRIPT = """
function(local, session, clearFirst) {
  const apply = (store, entries) => {
    if (clearFirst) store.clear();
    let written = 0;
    for (const entry of entries) {
      try {
        store.setItem(entry.name, entry.value);
        written += 1;
      } catch (error) {
        // 配额超限或隐私模式下单项失败不应中断其余项。
      }
    }
    return written;
  };
  return {
    origin: location.origin,
    localStorage: apply(localStorage, local || []),
    sessionStorage: apply(sessionStorage, session || []),
  };
}
"""


class StateSession(Protocol):
    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...


class StateFrame(Protocol):
    async def call_on_document(
        self,
        declaration: str,
        arguments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


async def export_state(
    session: StateSession,
    frame: StateFrame,
    *,
    urls: list[str],
) -> dict[str, Any]:
    """导出当前会话态；Cookie 按授权 URL 收敛，Web Storage 取当前页面 origin。"""

    result = await session.call("Network.getCookies", {"urls": urls})
    raw_cookies = result.get("cookies")
    cookies: list[dict[str, Any]] = []
    if isinstance(raw_cookies, list):
        for item in raw_cookies[:MAX_COOKIES]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            cookies.append(
                {
                    "name": item["name"],
                    "value": str(item.get("value", ""))[:MAX_VALUE_LENGTH],
                    "domain": str(item.get("domain", "")),
                    "path": str(item.get("path", "/")),
                    "expires": item.get("expires", -1),
                    "httpOnly": bool(item.get("httpOnly")),
                    "secure": bool(item.get("secure")),
                    "sameSite": item.get("sameSite"),
                }
            )

    dumped = await frame.call_on_document(
        _DUMP_SCRIPT,
        [{"value": MAX_STORAGE_ENTRIES}, {"value": MAX_VALUE_LENGTH}],
    )
    payload = dumped.get("result", {}).get("value")
    origins: list[dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("origin"), str):
        origin_entry = {
            "origin": payload["origin"],
            "localStorage": _entries(payload.get("localStorage")),
            "sessionStorage": _entries(payload.get("sessionStorage")),
        }
        if origin_entry["localStorage"] or origin_entry["sessionStorage"]:
            origins.append(origin_entry)
    return {"cookies": cookies, "origins": origins}


async def import_state(
    session: StateSession,
    frame: StateFrame,
    state: dict[str, Any],
    *,
    allowed_origins: set[str],
    clear_existing: bool = False,
) -> dict[str, Any]:
    """把快照写回当前会话；越过任务授权范围的条目直接跳过。"""

    cookies = state.get("cookies")
    applied_cookies = 0
    skipped_cookies: list[str] = []
    if isinstance(cookies, list):
        params = []
        for item in cookies[:MAX_COOKIES]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            url = _cookie_url(item, allowed_origins)
            if url is None or _origin_of(url) not in allowed_origins:
                skipped_cookies.append(item["name"])
                continue
            entry: dict[str, Any] = {
                "name": item["name"],
                "value": str(item.get("value", ""))[:MAX_VALUE_LENGTH],
                "url": url,
                "path": str(item.get("path", "/")) or "/",
                "httpOnly": bool(item.get("httpOnly")),
                "secure": bool(item.get("secure")),
            }
            if isinstance(item.get("domain"), str) and item["domain"]:
                entry["domain"] = item["domain"]
            expires = item.get("expires")
            if isinstance(expires, int | float) and expires > 0:
                entry["expires"] = float(expires)
            if item.get("sameSite") in {"Strict", "Lax", "None"}:
                entry["sameSite"] = item["sameSite"]
            params.append(entry)
        if params:
            await session.call("Network.setCookies", {"cookies": params})
            applied_cookies = len(params)

    current = await frame.call_on_document("function(){return location.origin;}")
    current_origin = current.get("result", {}).get("value")
    local_entries: list[dict[str, str]] = []
    session_entries: list[dict[str, str]] = []
    skipped_origins: list[str] = []
    for origin in state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        name = origin.get("origin")
        if not isinstance(name, str):
            continue
        # Web Storage 只能写进当前页面自己的 origin，其余条目留给调用方切页后再导入。
        if name != current_origin:
            skipped_origins.append(name)
            continue
        local_entries = _entries(origin.get("localStorage"))
        session_entries = _entries(origin.get("sessionStorage"))

    written = {"localStorage": 0, "sessionStorage": 0}
    if local_entries or session_entries or clear_existing:
        restored = await frame.call_on_document(
            _RESTORE_SCRIPT,
            [
                {"value": local_entries},
                {"value": session_entries},
                {"value": clear_existing},
            ],
        )
        payload = restored.get("result", {}).get("value")
        if isinstance(payload, dict):
            written = {
                "localStorage": int(payload.get("localStorage", 0)),
                "sessionStorage": int(payload.get("sessionStorage", 0)),
            }
    return {
        "cookies_applied": applied_cookies,
        "cookies_skipped": skipped_cookies,
        "storage_written": written,
        "origins_skipped": skipped_origins,
        "current_origin": current_origin,
    }


def write_state_file(state: dict[str, Any], directory: Path) -> Path:
    """把快照独占写入私有文件；里面通常含登录凭据，权限必须收紧。"""

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"storage-state-{os.getpid()}-{int(os.times().elapsed * 1000)}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    return path


def read_state_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"会话态文件不存在：{path}")
    if path.stat().st_size > MAX_STATE_BYTES:
        raise ValueError(f"会话态文件超过 {MAX_STATE_BYTES} 字节上限")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"会话态文件无法解析：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("会话态文件的顶层必须是对象")
    return payload


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    """给模型看的摘要：只给数量与键名，不给任何取值。"""

    cookies = state.get("cookies") or []
    origins = state.get("origins") or []
    return {
        "cookie_count": len(cookies) if isinstance(cookies, list) else 0,
        "cookie_names": [
            item["name"]
            for item in cookies[:50]
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        if isinstance(cookies, list)
        else [],
        "origins": [
            {
                "origin": origin.get("origin"),
                "local_storage_keys": len(origin.get("localStorage") or []),
                "session_storage_keys": len(origin.get("sessionStorage") or []),
            }
            for origin in origins[:MAX_ORIGINS]
            if isinstance(origin, dict)
        ]
        if isinstance(origins, list)
        else [],
    }


def _entries(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, str]] = []
    for item in raw[:MAX_STORAGE_ENTRIES]:
        if not isinstance(item, dict):
            continue
        name, value = item.get("name"), item.get("value")
        if isinstance(name, str) and isinstance(value, str):
            entries.append({"name": name, "value": value[:MAX_VALUE_LENGTH]})
    return entries


def _cookie_url(cookie: dict[str, Any], allowed_origins: set[str]) -> str | None:
    """按 Cookie 的域反推一个可用于 setCookie 的 URL。

    Cookie 的 domain 不带端口和协议，直接拼出的 `http://127.0.0.1/` 会和带端口的授权
    origin 对不上，于是整批 Cookie 被误判为越权。所以优先复用主机名相同的授权 origin，
    把它的协议与端口带回来。
    """

    domain = str(cookie.get("domain", "")).lstrip(".")
    if not domain:
        return None
    path = str(cookie.get("path", "/")) or "/"
    for origin in allowed_origins:
        host = urlsplit(origin).hostname or ""
        if host == domain or host.endswith(f".{domain}"):
            return f"{origin.rstrip('/')}{path}"
    scheme = "https" if cookie.get("secure") else "http"
    return f"{scheme}://{domain}{path}"


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"

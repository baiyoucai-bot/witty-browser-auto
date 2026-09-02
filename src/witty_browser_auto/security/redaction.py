"""统一脱敏工具，避免日志、证据和记忆泄露认证信息。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "***已脱敏***"
_SENSITIVE_KEY = re.compile(
    r"(^|[-_])(authorization|cookie|set-cookie|password|passwd|token|api[-_]?key|secret)($|[-_])",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "code",
    "id_token",
    "keywords",
    "password",
    "refresh_token",
    "secret",
    "session",
    "ticket",
    "token",
}
TASK_INPUT_REDACTED = "[已隐藏任务输入]"


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key))


def redact_url(url: str, *, sensitive_values: Sequence[str] = ()) -> str:
    lowered = url.lower()
    if lowered.startswith("data:"):
        return "<内联数据 URL 已省略>"
    if lowered.startswith("blob:"):
        return "<Blob URL 已省略>"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<无效 URL>"

    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
    except ValueError:
        return "<无效 URL>"

    known_values = {item for item in sensitive_values if item}
    safe_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        should_mask = key.lower() in _SENSITIVE_QUERY_KEYS or value in known_values
        safe_query.append((key, REDACTED if should_mask else value))
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(safe_query), ""))


def sanitize_url_for_storage(url: str, inputs: Mapping[str, Any]) -> str:
    """删除敏感或等于任务输入的查询参数，使检查点和记忆可安全复用。"""

    sensitive_values = {str(item) for item in inputs.values() if str(item)}
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<无效 URL>"
    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
    except ValueError:
        return "<无效 URL>"
    safe_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _SENSITIVE_QUERY_KEYS and value not in sensitive_values
    ]
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(safe_query), ""))


def redact(value: Any, *, key: str | None = None) -> Any:
    if key and is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): redact(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, str):
        if value.startswith("data:image/"):
            return REDACTED
        if value.startswith(("http://", "https://")):
            return redact_url(value)
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value


def redact_task_inputs(value: Any, inputs: Mapping[str, Any]) -> Any:
    """递归隐藏任务输入原值，并继续应用通用密钥与 URL 脱敏。"""

    sensitive_values = tuple(
        sorted(
            {str(item) for item in inputs.values() if str(item)},
            key=len,
            reverse=True,
        )
    )

    def scrub(item: Any, *, key: str | None = None) -> Any:
        if key and is_sensitive_key(key):
            return REDACTED
        if isinstance(item, Mapping):
            return {
                scrub(str(item_key)): scrub(item_value, key=str(item_key))
                for item_key, item_value in item.items()
            }
        if isinstance(item, str):
            if item.startswith("data:image/"):
                return REDACTED
            if item.startswith(("http://", "https://")):
                item = redact_url(item, sensitive_values=sensitive_values)
            for sensitive in sensitive_values:
                item = item.replace(sensitive, TASK_INPUT_REDACTED)
            return item
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return [scrub(nested) for nested in item]
        return item

    return scrub(value)

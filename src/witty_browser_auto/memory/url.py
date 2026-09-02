"""URL 规范化、敏感参数过滤和路径模板生成。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from witty_browser_auto.domain.errors import ConfigurationError

_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "apikey",
    "api_key",
    "auth",
    "code",
    "id_token",
    "keywords",
    "key",
    "password",
    "refresh_token",
    "secret",
    "session",
    "ticket",
    "signature",
    "token",
}
_TRACKING_PREFIXES = ("utm_",)
_UUID_SEGMENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_LONG_IDENTIFIER = re.compile(r"^(?:\d{4,}|[0-9a-f]{16,})$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class NormalizedUrl:
    exact: str
    origin: str
    path: str
    path_template: str


def normalize_url(url: str, *, retained_query_keys: frozenset[str] = frozenset()) -> NormalizedUrl:
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ConfigurationError("URL 格式无效", context={"url": url}) from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigurationError("URL 记忆只支持 http 或 https 地址", context={"url": url})

    hostname = parts.hostname.lower().encode("idna").decode("ascii")
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    # 保留 URL 结构字符，同时统一非 ASCII 的编码形式。
    path = quote(path, safe="/%:@-._~!$&'()*+,;=")

    retained = {item.lower() for item in retained_query_keys}
    safe_query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        normalized_key = key.lower()
        # 认证类查询参数无条件删除，调用方不能通过保留列表绕过记忆安全边界。
        if normalized_key in _SENSITIVE_QUERY_KEYS:
            continue
        if normalized_key.startswith(_TRACKING_PREFIXES) and normalized_key not in retained:
            continue
        safe_query.append((key, value))
    safe_query.sort(key=lambda item: (item[0], item[1]))

    origin = f"{scheme}://{netloc}"
    exact = urlunsplit((scheme, netloc, path, urlencode(safe_query, doseq=True), ""))
    return NormalizedUrl(
        exact=exact,
        origin=origin,
        path=path,
        path_template=template_path(path),
    )


def template_path(path: str) -> str:
    segments = path.split("/")
    normalized = []
    for segment in segments:
        if _UUID_SEGMENT.fullmatch(segment) or _LONG_IDENTIFIER.fullmatch(segment):
            normalized.append("{id}")
        else:
            normalized.append(segment)
    return "/".join(normalized) or "/"

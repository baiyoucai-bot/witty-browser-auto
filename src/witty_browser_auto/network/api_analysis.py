"""把同一个接口的多次交换归纳成可直接照着写代码的接口契约。

单看一条请求只能复制它；要写出能跑通别的参数、能翻页的代码，必须知道 URL 里哪一段
是可变的、哪些 query 参数是入参、鉴权凭据放在哪、正文和响应长什么样。这里把同
endpoint 的多次交换合并后推断这些结论，模型据此写代码而不是靠猜。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from witty_browser_auto.network.traffic import NetworkExchange
from witty_browser_auto.security.redaction import REDACTED, is_sensitive_key

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_LONG_HEX = re.compile(r"^[0-9a-f]{16,}$", re.I)
_DIGITS = re.compile(r"^\d+$")

_PAGINATION_PARAMS = frozenset(
    {
        "page",
        "pageno",
        "page_no",
        "pagenum",
        "page_num",
        "pageindex",
        "page_index",
        "offset",
        "skip",
        "start",
        "from",
        "cursor",
        "after",
        "before",
        "limit",
        "size",
        "pagesize",
        "page_size",
        "per_page",
        "perpage",
        "rows",
        "pagecount",
    }
)
_SORT_PARAMS = frozenset(
    {"sort", "sortby", "sort_by", "order", "orderby", "order_by", "asc", "desc", "direction"}
)
_TIME_PARAMS = frozenset({"timestamp", "ts", "_t", "_", "time", "date", "starttime", "endtime"})
_RESPONSE_TOTAL_KEYS = frozenset(
    {
        "total",
        "totalcount",
        "total_count",
        "count",
        "totalsize",
        "total_size",
        "totalnum",
        "records",
    }
)
_RESPONSE_PAGING_KEYS = frozenset(
    {
        "haspage",
        "hasmore",
        "has_more",
        "hasnext",
        "has_next",
        "nextcursor",
        "next_cursor",
        "next",
        "nextpage",
        "next_page",
        "totalpages",
        "total_pages",
        "pagecount",
        "page_count",
    }
)

_MAX_SCHEMA_DEPTH = 5
_MAX_SCHEMA_KEYS = 40
_MAX_SAMPLE_CHARS = 120
_MAX_PARAM_SAMPLES = 5


# ----------------------------------------------------------------------
# URL 归一
# ----------------------------------------------------------------------


def path_template(path: str) -> str:
    """把路径里的标识段替换成占位符，得到可复用的 URL 模板。"""

    segments = []
    for segment in path.split("/"):
        if not segment:
            segments.append(segment)
        elif _DIGITS.match(segment):
            segments.append("{id}")
        elif _UUID.match(segment):
            segments.append("{uuid}")
        elif _LONG_HEX.match(segment):
            segments.append("{hash}")
        else:
            segments.append(segment)
    return "/".join(segments) or "/"


def endpoint_signature(exchange: NetworkExchange) -> tuple[str, str, str]:
    parts = urlsplit(exchange.url)
    return (
        exchange.method.upper(),
        f"{parts.scheme}://{parts.netloc}",
        path_template(parts.path or "/"),
    )


# ----------------------------------------------------------------------
# Schema 推断
# ----------------------------------------------------------------------


def _sample_of(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_MAX_SAMPLE_CHARS]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return None


def infer_schema(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """推断 JSON 值的结构；深度与字段数有界，避免把整份响应搬进上下文。"""

    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean", "sample": value}
    if isinstance(value, int):
        return {"type": "integer", "sample": value}
    if isinstance(value, float):
        return {"type": "number", "sample": value}
    if isinstance(value, str):
        return {"type": "string", "sample": _sample_of(value), "length": len(value)}
    if isinstance(value, list):
        node: dict[str, Any] = {"type": "array", "length": len(value)}
        if value and depth < _MAX_SCHEMA_DEPTH:
            node["item"] = infer_schema(value[0], depth=depth + 1)
        return node
    if isinstance(value, Mapping):
        node = {"type": "object", "field_count": len(value)}
        if depth >= _MAX_SCHEMA_DEPTH:
            node["fields"] = {}
            node["truncated"] = True
            return node
        fields: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_SCHEMA_KEYS:
                node["truncated"] = True
                break
            fields[str(key)] = infer_schema(item, depth=depth + 1)
        node["fields"] = fields
        return node
    return {"type": "unknown"}


def find_record_path(value: Any, *, path: tuple[str, ...] = ()) -> tuple[list[str], int]:
    """找出响应里最长的对象数组，作为批量数据的记录路径。"""

    best: tuple[list[str], int] = ([], 0)
    if isinstance(value, list):
        if value and isinstance(value[0], Mapping):
            best = (list(path), len(value))
    elif isinstance(value, Mapping) and len(path) < _MAX_SCHEMA_DEPTH:
        for key, item in value.items():
            candidate = find_record_path(item, path=(*path, str(key)))
            if candidate[1] > best[1]:
                best = candidate
    return best


# ----------------------------------------------------------------------
# 参数与鉴权
# ----------------------------------------------------------------------


def classify_param(name: str) -> str:
    lowered = name.lower()
    if lowered in _PAGINATION_PARAMS:
        return "pagination"
    if lowered in _SORT_PARAMS:
        return "sort"
    if lowered in _TIME_PARAMS:
        return "timestamp"
    if is_sensitive_key(lowered) or lowered in {"sign", "signature", "nonce", "sid"}:
        return "credential"
    return "filter"


def _infer_value_type(value: str) -> str:
    if _DIGITS.match(value):
        return "integer"
    if value.lower() in {"true", "false"}:
        return "boolean"
    return "string"


def _collect_query_params(exchanges: Sequence[NetworkExchange]) -> list[dict[str, Any]]:
    observed: dict[str, list[str]] = {}
    present_in: dict[str, int] = {}
    for exchange in exchanges:
        pairs = parse_qsl(urlsplit(exchange.url).query, keep_blank_values=True)
        seen: set[str] = set()
        for name, value in pairs:
            observed.setdefault(name, [])
            if len(observed[name]) < _MAX_PARAM_SAMPLES and value not in observed[name]:
                observed[name].append(value)
            seen.add(name)
        for name in seen:
            present_in[name] = present_in.get(name, 0) + 1
    total = len(exchanges)
    params = []
    for name, samples in observed.items():
        params.append(
            {
                "name": name,
                "role": classify_param(name),
                "value_type": _infer_value_type(samples[0]) if samples else "string",
                "samples": [item[:_MAX_SAMPLE_CHARS] for item in samples],
                "varies": len(samples) > 1,
                "always_present": present_in.get(name, 0) == total,
            }
        )
    params.sort(key=lambda item: item["name"])
    return params


def _detect_auth(exchanges: Sequence[NetworkExchange]) -> dict[str, Any]:
    """定位凭据的携带位置；只记录位置和方案，不复制凭据本身。"""

    schemes: list[str] = []
    cookie_names: list[str] = []
    header_names: list[str] = []
    query_names: list[str] = []
    for exchange in exchanges:
        for name, value in exchange.request_headers.items():
            lowered = name.lower()
            if lowered == "authorization":
                scheme = value.split(" ", 1)[0] if " " in value else "opaque"
                if scheme not in schemes:
                    schemes.append(scheme)
                if name not in header_names:
                    header_names.append(name)
            elif lowered == "cookie":
                for cookie in value.split(";"):
                    key = cookie.split("=", 1)[0].strip()
                    if key and key not in cookie_names:
                        cookie_names.append(key)
            elif is_sensitive_key(lowered) or lowered in {"x-csrf-token", "x-api-key"}:
                if name not in header_names:
                    header_names.append(name)
        for name, _ in parse_qsl(urlsplit(exchange.url).query, keep_blank_values=True):
            if classify_param(name) == "credential" and name not in query_names:
                query_names.append(name)
    return {
        "authorization_schemes": schemes,
        "credential_headers": sorted(header_names),
        "cookie_names": sorted(cookie_names)[:30],
        "credential_query_params": sorted(query_names),
        "requires_cookies": bool(cookie_names),
    }


# ----------------------------------------------------------------------
# 正文
# ----------------------------------------------------------------------


def _parse_json(text: str | None) -> Any:
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def _content_type(headers: Mapping[str, str]) -> str:
    for name, value in headers.items():
        if name.lower() == "content-type":
            return value.split(";", 1)[0].strip().lower()
    return ""


def _describe_request_body(exchange: NetworkExchange) -> dict[str, Any]:
    body = exchange.request_body
    content_type = _content_type(exchange.request_headers)
    if body is None or body.text is None:
        return {"present": False, "content_type": content_type}
    if body.base64_encoded:
        return {
            "present": True,
            "content_type": content_type,
            "format": "binary",
            "byte_length": body.byte_length,
        }
    parsed = _parse_json(body.text)
    if parsed is not None:
        graphql = _describe_graphql(parsed)
        payload: dict[str, Any] = {
            "present": True,
            "content_type": content_type or "application/json",
            "format": "json",
            "schema": infer_schema(parsed),
        }
        if graphql is not None:
            payload["graphql"] = graphql
        return payload
    if "x-www-form-urlencoded" in content_type:
        pairs = parse_qsl(body.text, keep_blank_values=True)
        return {
            "present": True,
            "content_type": content_type,
            "format": "form",
            "fields": [
                {"name": name, "value_type": _infer_value_type(value)} for name, value in pairs
            ],
        }
    return {
        "present": True,
        "content_type": content_type,
        "format": "raw",
        "byte_length": body.byte_length,
    }


def _describe_graphql(parsed: Any) -> dict[str, Any] | None:
    if not isinstance(parsed, Mapping) or "query" not in parsed:
        return None
    query = parsed.get("query")
    if not isinstance(query, str):
        return None
    variables = parsed.get("variables")
    return {
        "operation_name": parsed.get("operationName"),
        "query_preview": query[:400],
        "variable_names": sorted(variables) if isinstance(variables, Mapping) else [],
    }


def _describe_response(exchange: NetworkExchange) -> dict[str, Any]:
    body = exchange.response_body
    payload: dict[str, Any] = {
        "status": exchange.status,
        "mime_type": exchange.mime_type,
        "content_type": _content_type(exchange.response_headers),
    }
    if body is None or body.text is None:
        payload["available"] = False
        payload["reason"] = body.reason if body is not None else "未捕获响应正文"
        return payload
    payload["available"] = True
    payload["byte_length"] = body.byte_length
    parsed = _parse_json(body.text)
    if parsed is None:
        payload["format"] = "text"
        return payload
    payload["format"] = "json"
    payload["schema"] = infer_schema(parsed)
    record_path, record_count = find_record_path(parsed)
    if record_count:
        payload["record_path"] = record_path
        payload["record_count"] = record_count
        cursor: Any = parsed
        for key in record_path:
            cursor = cursor[key]
        if cursor and isinstance(cursor[0], Mapping):
            payload["record_fields"] = sorted(str(key) for key in cursor[0])[:_MAX_SCHEMA_KEYS]
    if isinstance(parsed, Mapping):
        payload["total_fields"] = [
            str(key) for key in parsed if str(key).lower().replace("-", "_") in _RESPONSE_TOTAL_KEYS
        ]
        payload["pagination_fields"] = [
            str(key)
            for key in parsed
            if str(key).lower().replace("-", "_") in _RESPONSE_PAGING_KEYS
        ]
    return payload


# ----------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------


def analyze_endpoint(exchanges: Sequence[NetworkExchange]) -> dict[str, Any]:
    """把同 endpoint 的若干次交换归纳成接口契约。"""

    if not exchanges:
        raise ValueError("没有可分析的流量交换")
    ordered = sorted(exchanges, key=lambda item: item.started_wall)
    representative = next(
        (item for item in reversed(ordered) if item.status and 200 <= item.status < 300),
        ordered[-1],
    )
    method, origin, template = endpoint_signature(representative)
    params = _collect_query_params(ordered)
    pagination = [item for item in params if item["role"] == "pagination"]
    response = _describe_response(representative)
    return {
        "endpoint": {
            "method": method,
            "origin": origin,
            "path_template": template,
            "url_template": f"{origin}{template}",
            "sample_url": representative.url,
        },
        "sample_count": len(ordered),
        "sample_exchange_id": representative.exchange_id,
        "status_codes": sorted({item.status for item in ordered if item.status is not None}),
        "query_params": params,
        "auth": _detect_auth(ordered),
        "request_headers": dict(representative.request_headers),
        "request_body": _describe_request_body(representative),
        "response": response,
        "pagination": {
            "request_params": [item["name"] for item in pagination],
            "response_fields": response.get("pagination_fields", []),
            "total_fields": response.get("total_fields", []),
            "strategy": _pagination_strategy(pagination),
        },
        "timing_ms": representative.timing.total_ms,
    }


def _pagination_strategy(pagination: Sequence[Mapping[str, Any]]) -> str:
    names = {str(item["name"]).lower() for item in pagination}
    if names & {"cursor", "after", "before"}:
        return "cursor"
    if names & {"offset", "skip", "start", "from"}:
        return "offset"
    if names & {"page", "pageno", "page_no", "pagenum", "page_num", "pageindex", "page_index"}:
        return "page_number"
    return "none"


def strip_samples(node: Any) -> Any:
    """去掉 schema 里的真实取值，只留结构。

    响应正文里就是业务数据本身。模型拿到结构即可写代码；把 sample 一并给它，等于
    开了一条绕过批量采集完整性门、逐条读取业务数据的旁路。
    """

    if isinstance(node, Mapping):
        return {
            str(key): strip_samples(value) for key, value in node.items() if str(key) != "sample"
        }
    if isinstance(node, list):
        return [strip_samples(item) for item in node]
    return node


def model_view(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """模型视图：保留结构与参数名，隐去凭据值、Header 值与业务数据取值。"""

    params = []
    for item in analysis.get("query_params", []):
        row = dict(item)
        if row.get("role") == "credential":
            row["samples"] = [REDACTED]
        params.append(row)
    response = dict(analysis.get("response", {}))
    if "schema" in response:
        response["schema"] = strip_samples(response["schema"])
    request_body = dict(analysis.get("request_body", {}))
    if "schema" in request_body:
        request_body["schema"] = strip_samples(request_body["schema"])
    return {
        "endpoint": analysis.get("endpoint", {}),
        "sample_count": analysis.get("sample_count"),
        "sample_exchange_id": analysis.get("sample_exchange_id"),
        "status_codes": analysis.get("status_codes", []),
        "query_params": params,
        "auth": analysis.get("auth", {}),
        "request_header_names": sorted(analysis.get("request_headers", {})),
        "request_body": request_body,
        "response": response,
        "pagination": analysis.get("pagination", {}),
        "note": "Header 值、凭据与响应取值只回给调用方进程，模型侧只提供结构与名称",
    }

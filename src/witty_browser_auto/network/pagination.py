"""沿接口分页主动取全数据，并给出可核对的闭合证据。

`analyze_api_endpoint` 已经能推断分页策略，`replay_network_request` 能带着登录态重放
一条请求，但两者之间一直缺一段：没有东西沿着分页把数据真正取全。结果是"看得清接口、
写得出代码，却拿不到完整数据"，仍要靠人在浏览器里一页页点。

这里补上那一段。三条规则决定了实现形态：

- **不声称闭合就不算完成。** 采集结果必须带正面证据——收齐数等于服务端声明的总数，
  或者末页确实短于整页。任何一页失败、或只是"跑到了页数上限"，一律 `closed=False`
  并说明缺口，绝不把"抓了一些"包装成"抓全了"。
- **必须能识破服务端忽略分页参数。** 参数名猜错时服务端通常照返第一页，天真的循环会
  一直抓到页数上限并自认为成功。判据是"这一页有没有带来新记录"，零新增即停并报错。
- **记录只回给调用方。** 响应正文就是业务数据本身，给模型等于开一条绕过采集完整性门
  逐条读数的旁路，与 `analyze_api_endpoint` 剥离 sample 取值是同一条界线。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

STRATEGIES: tuple[str, ...] = ("page_number", "offset", "cursor")
PAGE_LOCATIONS: tuple[str, ...] = ("query", "body")
CURSOR_SOURCES: tuple[str, ...] = ("body", "header", "link")

MAX_PAGES = 200
MAX_RECORDS = 100_000
DEFAULT_MAX_PAGES = 50

_PAGE_PARAMS = ("page", "pageno", "page_no", "pagenum", "page_num", "pageindex", "page_index")
_OFFSET_PARAMS = ("offset", "skip", "start", "from")
_CURSOR_PARAMS = ("cursor", "after", "next", "next_cursor", "nextcursor", "page_token")
_SIZE_PARAMS = ("size", "limit", "pagesize", "page_size", "per_page", "perpage", "rows", "count")
_CURSOR_RESPONSE_FIELDS = (
    "next_cursor",
    "nextCursor",
    "next",
    "cursor",
    "next_page_token",
    "nextPageToken",
    "after",
)
_TOTAL_FIELDS = (
    "total",
    "totalCount",
    "total_count",
    "totalSize",
    "total_size",
    "count",
    "records",
)
_MAX_BODY_FLATTEN_DEPTH = 4
# GitHub 式分页：`Link: <https://…?after=x>; rel="next", <…>; rel="last"`。
# `[^,]*` 阻止跨过逗号，避免把上一段的 URL 与下一段的 rel 拼在一起。
_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*[^,]*\brel\s*=\s*"?next"?', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PaginationPlan:
    """一次分页遍历的完整计划。"""

    strategy: str
    param: str
    start: int
    step: int
    page_size: int | None
    record_path: tuple[str, ...]
    cursor_field: str | None = None
    # 分页字段所在位置：query 改写 URL，body 改写 JSON/表单请求体。
    page_in: str = "query"
    # 游标来源：body 从响应 JSON 取，header 从指定响应头取，link 直接用 Link 头给的下一页 URL。
    cursor_source: str = "body"
    cursor_header: str = ""

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "strategy": self.strategy,
            "param": self.param,
            "start": self.start,
            "record_path": list(self.record_path),
            "page_in": self.page_in,
        }
        if self.strategy == "offset":
            payload["step"] = self.step
        if self.page_size is not None:
            payload["page_size"] = self.page_size
        if self.cursor_field:
            payload["cursor_field"] = self.cursor_field
        if self.strategy == "cursor":
            payload["cursor_source"] = self.cursor_source
            if self.cursor_header:
                payload["cursor_header"] = self.cursor_header
        return payload


@dataclass(slots=True)
class PageAttempt:
    index: int
    url: str
    status: int | None = None
    records: int = 0
    new_records: int = 0
    error: str = ""

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "index": self.index,
            "url": self.url,
            "status": self.status,
            "records": self.records,
            "new_records": self.new_records,
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(slots=True)
class CollectionOutcome:
    plan: PaginationPlan
    records: list[Any] = field(default_factory=list)
    pages: list[PageAttempt] = field(default_factory=list)
    declared_total: int | None = None
    closed: bool = False
    reason: str = ""

    def evidence(self) -> dict[str, Any]:
        failed = [page.public_dict() for page in self.pages if page.error]
        return {
            "closed": self.closed,
            "reason": self.reason,
            "declared_total": self.declared_total,
            "collected": len(self.records),
            "pages_fetched": len(self.pages),
            "failed_pages": failed,
            "plan": self.plan.public_dict(),
            "pages": [page.public_dict() for page in self.pages],
        }


def build_plan(
    *,
    sample_url: str,
    analysis: Mapping[str, Any],
    overrides: Mapping[str, Any],
    sample_body: str | None = None,
) -> PaginationPlan:
    """结合接口契约、样本 URL/请求体与显式覆盖，定出遍历计划。"""

    query = dict(parse_qsl(urlsplit(sample_url).query, keep_blank_values=True))
    pagination = analysis.get("pagination") if isinstance(analysis, Mapping) else {}
    pagination = pagination if isinstance(pagination, Mapping) else {}

    page_in = str(overrides.get("page_in") or "query")
    if page_in not in PAGE_LOCATIONS:
        raise ValueError(f"page_in 必须是 {' 或 '.join(PAGE_LOCATIONS)}")
    cursor_source = str(overrides.get("cursor_in") or "body")
    if cursor_source not in CURSOR_SOURCES:
        raise ValueError(f"cursor_in 必须是 {'、'.join(CURSOR_SOURCES)}")
    cursor_header = str(overrides.get("cursor_header") or "")
    if cursor_source == "header" and not cursor_header:
        raise ValueError("cursor_in=header 必须提供 cursor_header")

    # 分页字段在请求体里时，猜参数、取起点、读每页大小全部改用展平后的请求体字段。
    fields = query
    if page_in == "body":
        fields = _body_fields(sample_body)
        if not fields:
            raise ValueError("page_in=body 要求来源请求带可解析的 JSON 对象或表单请求体")

    explicit_strategy = overrides.get("strategy")
    strategy = str(explicit_strategy or pagination.get("strategy") or "none")
    if page_in == "body" and not explicit_strategy:
        # 契约里的策略是按查询参数推断的，对请求体分页不成立。
        strategy = "none"
    if cursor_source != "body" and not explicit_strategy:
        # cursor_in 只对游标分页有意义，指定它本身就表明这是游标分页。
        strategy = "cursor"
    if cursor_source != "body" and explicit_strategy and strategy != "cursor":
        raise ValueError("cursor_in 只在 strategy=cursor 时有意义")

    param = str(overrides.get("page_param") or "") or _guess_param(fields, strategy)
    # 游标由 Link 响应头给出下一页完整 URL 时，本地不需要任何分页参数。
    if strategy not in STRATEGIES or (not param and cursor_source != "link"):
        guessed_strategy, guessed_param = _guess_from_query(fields)
        if guessed_strategy in STRATEGIES:
            strategy, param = guessed_strategy, guessed_param
    if strategy not in STRATEGIES:
        raise ValueError(
            "无法确定分页策略，请显式提供 strategy 与 page_param；"
            f"样本{'请求体' if page_in == 'body' else ' URL'}的字段：{sorted(fields) or '无'}"
        )
    link_driven = strategy == "cursor" and cursor_source == "link"
    if not param and not link_driven:
        raise ValueError(f"策略 {strategy} 找不到对应的分页参数，请显式提供 page_param")

    page_size = _resolve_page_size(fields, overrides)
    record_path = _resolve_record_path(analysis, overrides)
    start = _resolve_start(fields, param, strategy, overrides)
    # offset 每页前进一个整页；页码策略每次加一。
    step = int(overrides.get("step") or page_size or 0) if strategy == "offset" else 1
    if strategy == "offset" and step <= 0:
        raise ValueError("offset 策略必须能确定每页大小，请提供 page_size 或 step")

    cursor_field = overrides.get("cursor_field")
    if strategy == "cursor" and cursor_source == "body" and not cursor_field:
        cursor_field = _guess_cursor_field(analysis)
    return PaginationPlan(
        strategy=strategy,
        param=param,
        start=start,
        step=step,
        page_size=page_size,
        record_path=tuple(record_path),
        cursor_field=cursor_field,
        page_in=page_in,
        cursor_source=cursor_source,
        cursor_header=cursor_header,
    )


def page_url(plan: PaginationPlan, sample_url: str, index: int, cursor: str | None) -> str:
    """按计划算出第 index 页的 URL，index 从 0 起。"""

    if plan.page_in == "body":
        # 分页字段在请求体里，URL 保持原样，原有过滤条件一并沿用。
        return sample_url
    parts = urlsplit(sample_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if plan.strategy == "cursor":
        if not plan.param:
            # 游标来自 Link 响应头，下一页 URL 由服务端给出，这里只负责首页。
            return sample_url
        if index == 0:
            query.pop(plan.param, None)
        else:
            query[plan.param] = cursor or ""
    else:
        query[plan.param] = str(plan.start + index * plan.step)
    if plan.page_size is not None:
        for name in _SIZE_PARAMS:
            if name in query:
                query[name] = str(plan.page_size)
                break
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=False), parts.fragment)
    )


def page_body(
    plan: PaginationPlan,
    sample_body: str | None,
    index: int,
    cursor: str | None,
) -> str | None:
    """按计划改写请求体里的分页字段；分页不在请求体时返回 None。

    POST 接口把页码放在 JSON 体(含嵌套对象，参数名用点号表示路径)或表单体里很常见，
    这类接口没法靠改写 URL 翻页。改写时保留原字段的类型：服务端期望数字却收到字符串
    通常直接 400。
    """

    if plan.page_in != "body":
        return None
    if not sample_body:
        raise ValueError("请求体承载分页时来源请求必须带请求体")
    value: Any = cursor if plan.strategy == "cursor" else plan.start + index * plan.step
    try:
        payload = json.loads(sample_body)
    except ValueError:
        payload = None
    if isinstance(payload, MutableMapping):
        return json.dumps(_rewrite_json_body(payload, plan, value, index), ensure_ascii=False)
    pairs = parse_qsl(sample_body, keep_blank_values=True)
    if not pairs:
        raise ValueError("来源请求体既不是 JSON 对象也不是表单编码，无法改写分页字段")
    return _rewrite_form_body(pairs, plan, value, index)


def extract_cursor_from_headers(headers: Mapping[str, str], header_name: str) -> str | None:
    """从指定响应头读游标；有的接口把游标放在 `X-Next-Cursor` 这类头里而不是正文。"""

    if not header_name:
        return None
    target = header_name.casefold()
    for name, value in headers.items():
        if name.casefold() == target and isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_next_link(headers: Mapping[str, str]) -> str | None:
    """解析 `Link: <url>; rel="next"` 响应头，返回下一页的完整 URL。"""

    for name, value in headers.items():
        if name.casefold() != "link" or not isinstance(value, str):
            continue
        match = _LINK_NEXT.search(value)
        if match:
            return match.group(1).strip()
    return None


def extract_records(payload: Any, record_path: Sequence[str]) -> list[Any]:
    node = payload
    for key in record_path:
        if isinstance(node, Mapping) and key in node:
            node = node[key]
        else:
            return []
    return list(node) if isinstance(node, list) else []


def extract_total(payload: Any, override: Sequence[str] | None = None) -> int | None:
    """读出服务端声明的总数；没有就返回 None，而不是拿收集数冒充。"""

    if override:
        node: Any = payload
        for key in override:
            if isinstance(node, Mapping) and key in node:
                node = node[key]
            else:
                return None
        return int(node) if isinstance(node, int) and not isinstance(node, bool) else None
    if not isinstance(payload, Mapping):
        return None
    for name in _TOTAL_FIELDS:
        value = payload.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def extract_cursor(payload: Any, field_name: str | None) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    names = (field_name,) if field_name else _CURSOR_RESPONSE_FIELDS
    for name in names:
        if not name:
            continue
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        # 有的接口把游标塞在 paging/pageInfo 这类子对象里。
        if isinstance(value, Mapping):
            nested = extract_cursor(value, None)
            if nested:
                return nested
    for container in ("paging", "pageInfo", "page_info", "meta"):
        nested_payload = payload.get(container)
        if isinstance(nested_payload, Mapping):
            nested = extract_cursor(nested_payload, field_name)
            if nested:
                return nested
    return None


def record_fingerprint(record: Any, dedupe_key: str | None) -> str:
    if dedupe_key and isinstance(record, Mapping) and dedupe_key in record:
        return f"k:{record[dedupe_key]!r}"
    try:
        canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        canonical = repr(record)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decide_closure(outcome: CollectionOutcome, *, exhausted_budget: bool) -> None:
    """判定是否闭合；只有正面证据才算数。"""

    if any(page.error for page in outcome.pages):
        outcome.closed = False
        failed = next(page for page in outcome.pages if page.error)
        outcome.reason = f"第 {failed.index} 页请求失败：{failed.error}"
        return
    collected = len(outcome.records)
    if outcome.declared_total is not None:
        if collected == outcome.declared_total:
            outcome.closed = True
            outcome.reason = f"收齐 {collected} 条，与服务端声明的总数一致"
        else:
            outcome.closed = False
            outcome.reason = (
                f"服务端声明 {outcome.declared_total} 条，实际收到 {collected} 条，"
                f"相差 {outcome.declared_total - collected} 条"
            )
        return
    if exhausted_budget:
        outcome.closed = False
        outcome.reason = f"达到页数上限仍未走到末页，已收 {collected} 条，无法确认取全"
        return
    last = outcome.pages[-1] if outcome.pages else None
    if last is not None and outcome.plan.page_size and last.records < outcome.plan.page_size:
        outcome.closed = True
        outcome.reason = (
            f"末页只返回 {last.records} 条，少于每页 {outcome.plan.page_size} 条，"
            f"共收 {collected} 条"
        )
        return
    if last is not None and last.records == 0:
        outcome.closed = True
        outcome.reason = f"翻到空页，共收 {collected} 条"
        return
    outcome.closed = False
    outcome.reason = f"服务端未声明总数且末页判据不成立，已收 {collected} 条，无法确认取全"


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------


def _rewrite_json_body(
    payload: Mapping[str, Any],
    plan: PaginationPlan,
    value: Any,
    index: int,
) -> dict[str, Any]:
    updated = copy.deepcopy(dict(payload))
    path = plan.param.split(".") if plan.param else []
    parent = _json_parent(updated, path)
    if parent is None:
        raise ValueError(f"请求体里找不到分页字段 {plan.param} 的父级对象")
    leaf = path[-1]
    if plan.strategy == "cursor" and index == 0:
        # 首页不带游标，与查询串策略保持一致。
        parent.pop(leaf, None)
    else:
        parent[leaf] = _coerce_like(parent.get(leaf), value, plan.strategy)
    if plan.page_size is not None:
        _rewrite_size_field(parent, plan.page_size)
        if parent is not updated:
            _rewrite_size_field(updated, plan.page_size)
    return updated


def _json_parent(node: Any, path: Sequence[str]) -> MutableMapping[str, Any] | None:
    """定位分页字段的父级对象；中间层缺失时返回 None 而不是凭空造出结构。"""

    if not path:
        return None
    for key in path[:-1]:
        if not isinstance(node, MutableMapping) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, MutableMapping) else None


def _coerce_like(existing: Any, value: Any, strategy: str) -> Any:
    """按原字段类型写回：数字字段写数字，字符串字段写字符串。"""

    if value is None:
        return ""
    if isinstance(existing, bool) or existing is None:
        return str(value) if strategy == "cursor" else value
    if isinstance(existing, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if isinstance(existing, str):
        return str(value)
    return value


def _rewrite_size_field(node: MutableMapping[str, Any], page_size: int) -> None:
    for name in tuple(node):
        if name.lower() in _SIZE_PARAMS:
            node[name] = _coerce_like(node[name], page_size, "page_number")
            return


def _rewrite_form_body(
    pairs: Sequence[tuple[str, str]],
    plan: PaginationPlan,
    value: Any,
    index: int,
) -> str:
    text = "" if value is None else str(value)
    skip_page = plan.strategy == "cursor" and index == 0
    result: list[tuple[str, str]] = []
    wrote_page = False
    for name, raw in pairs:
        if name.casefold() == plan.param.casefold():
            if skip_page:
                continue
            result.append((name, text))
            wrote_page = True
        elif plan.page_size is not None and name.lower() in _SIZE_PARAMS:
            result.append((name, str(plan.page_size)))
        else:
            result.append((name, raw))
    if not wrote_page and not skip_page:
        result.append((plan.param, text))
    return urlencode(result, doseq=False)


def _body_fields(sample_body: str | None) -> dict[str, str]:
    """把请求体展平成 {字段名: 文本值}，嵌套对象用点号连接。

    展平后就能把猜参数、取起点、读每页大小这几套查询串逻辑原样复用到请求体上。
    """

    if not sample_body:
        return {}
    try:
        payload = json.loads(sample_body)
    except ValueError:
        payload = None
    if isinstance(payload, Mapping):
        flat: dict[str, str] = {}
        _flatten_fields(payload, (), flat, 0)
        return flat
    return dict(parse_qsl(sample_body, keep_blank_values=True))


def _flatten_fields(
    node: Mapping[str, Any],
    prefix: tuple[str, ...],
    out: dict[str, str],
    depth: int,
) -> None:
    if depth > _MAX_BODY_FLATTEN_DEPTH:
        return
    for key, value in node.items():
        path = (*prefix, str(key))
        if isinstance(value, Mapping):
            _flatten_fields(value, path, out, depth + 1)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            out[".".join(path)] = str(value)


def _lowered_index(fields: Mapping[str, str]) -> dict[str, str]:
    """字段名到原名的小写索引；嵌套字段额外用末段建索引，便于猜出 query.pageNum。"""

    lowered: dict[str, str] = {}
    for name in fields:
        lowered.setdefault(name.lower(), name)
        lowered.setdefault(name.rsplit(".", 1)[-1].lower(), name)
    return lowered


def _guess_param(query: Mapping[str, str], strategy: str) -> str:
    names = {
        "page_number": _PAGE_PARAMS,
        "offset": _OFFSET_PARAMS,
        "cursor": _CURSOR_PARAMS,
    }.get(strategy, ())
    lowered = _lowered_index(query)
    for candidate in names:
        if candidate in lowered:
            return lowered[candidate]
    return ""


def _guess_from_query(query: Mapping[str, str]) -> tuple[str, str]:
    for strategy in STRATEGIES:
        param = _guess_param(query, strategy)
        if param:
            return strategy, param
    return "none", ""


def _resolve_page_size(query: Mapping[str, str], overrides: Mapping[str, Any]) -> int | None:
    explicit = overrides.get("page_size")
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0:
        return explicit
    lowered = _lowered_index(query)
    for candidate in _SIZE_PARAMS:
        name = lowered.get(candidate)
        raw = query.get(name) if name else None
        if raw is not None and raw.isdigit() and int(raw) > 0:
            return int(raw)
    return None


def _resolve_record_path(analysis: Mapping[str, Any], overrides: Mapping[str, Any]) -> list[str]:
    explicit = overrides.get("record_path")
    if isinstance(explicit, list):
        return [str(item) for item in explicit]
    response = analysis.get("response") if isinstance(analysis, Mapping) else {}
    if isinstance(response, Mapping):
        inferred = response.get("record_path")
        if isinstance(inferred, list):
            return [str(item) for item in inferred]
    # 空路径表示响应顶层就是数组。
    return []


def _resolve_start(
    query: Mapping[str, str],
    param: str,
    strategy: str,
    overrides: Mapping[str, Any],
) -> int:
    explicit = overrides.get("start")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit
    if strategy == "cursor":
        return 0
    raw = query.get(param, "")
    # 起点取样本自己的取值：有的接口页码从 0 起，有的从 1 起，猜错会漏首页或多抓一页。
    return int(raw) if raw.isdigit() else (0 if strategy == "offset" else 1)


def _guess_cursor_field(analysis: Mapping[str, Any]) -> str | None:
    response = analysis.get("response") if isinstance(analysis, Mapping) else {}
    if not isinstance(response, Mapping):
        return None
    fields = response.get("pagination_fields")
    if isinstance(fields, list):
        for name in fields:
            if str(name).lower().replace("-", "_") in {
                "next_cursor",
                "nextcursor",
                "next",
                "cursor",
                "next_page",
                "nextpage",
            }:
                return str(name)
    return None

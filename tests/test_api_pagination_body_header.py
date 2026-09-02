"""请求体承载分页与响应头游标的单元测试。

覆盖两类此前无法遍历的接口：页码放在 POST 的 JSON/表单请求体里，以及游标只出现在
响应头里，即自定义头或 GitHub 式 `Link: rel=next`。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

import pytest

from witty_browser_auto.config import NetworkTrafficConfig
from witty_browser_auto.network.inspection import NetworkTrafficInspector
from witty_browser_auto.network.pagination import (
    build_plan,
    extract_cursor_from_headers,
    extract_next_link,
    page_body,
)
from witty_browser_auto.network.traffic import NetworkBody, NetworkExchange, NetworkTrafficLog


def _exchange(
    url: str,
    *,
    method: str = "POST",
    request_body: str | None = None,
    response_body: str = "{}",
) -> NetworkExchange:
    exchange = NetworkExchange(
        exchange_id="ex-1",
        request_id="ex-1",
        session_id="session-1",
        method=method,
        url=url,
        status=200,
        state="finished",
    )
    exchange.request_headers = {"Content-Type": "application/json"}
    if request_body is not None:
        exchange.request_body = NetworkBody(
            text=request_body, byte_length=len(request_body.encode("utf-8"))
        )
        exchange.has_post_data = True
    exchange.response_body = NetworkBody(
        text=response_body, byte_length=len(response_body.encode("utf-8"))
    )
    exchange.mime_type = "application/json"
    return exchange


class _FakeInspector(NetworkTrafficInspector):
    """用固定数据集替换真实重放，并记录每页实际发出的 URL 与请求体。"""

    def __init__(self, log: NetworkTrafficLog, root: Path, *, serve: Any) -> None:
        super().__init__(log, root, config=NetworkTrafficConfig())
        self.serve = serve
        self.requested: list[tuple[str, str | None]] = []

    async def replay(self, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        url = str(arguments["url"])
        body = arguments.get("body")
        self.requested.append((url, body if body is None else str(body)))
        payload, headers = self.serve(url, body)
        return {
            "success": True,
            "status": 200,
            "json": payload,
            "headers": headers,
        }, {}

    @property
    def bodies(self) -> list[str | None]:
        return [body for _, body in self.requested]

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.requested]


# ----------------------------------------------------------------------
# 请求体承载分页
# ----------------------------------------------------------------------


def _json_body_inspector(tmp_path: Path, *, sample_body: str, total: int = 25) -> _FakeInspector:
    rows = [{"id": index} for index in range(total)]

    def serve(url: str, body: str | None) -> tuple[dict[str, Any], dict[str, str]]:
        payload = json.loads(body or "{}")
        node = payload.get("query", payload)
        page = int(node.get("pageNum", node.get("pageNo", 1)))
        size = int(node.get("pageSize", node.get("size", 10)))
        start = (page - 1) * size
        return {"items": rows[start : start + size], "total": total}, {}

    log = NetworkTrafficLog(config=NetworkTrafficConfig())
    log._exchanges["ex-1"] = _exchange(
        "https://example.com/api/list",
        request_body=sample_body,
        # 契约要从真实响应里推断 record_path，样本响应必须是完整形状。
        response_body=json.dumps({"items": rows[:10], "total": total}),
    )
    return _FakeInspector(log, tmp_path, serve=serve)


def test_json_body_pagination_walks_and_closes(tmp_path: Path) -> None:
    sample = json.dumps({"pageNum": 1, "pageSize": 10, "status": "paid"})
    inspector = _json_body_inspector(tmp_path, sample_body=sample)

    full, _ = asyncio.run(inspector.collect_pages({"exchange_id": "ex-1", "page_in": "body"}))

    assert full["closed"] is True
    assert full["collected"] == 25
    assert full["plan"]["page_in"] == "body"
    # URL 每页保持原样，翻页只体现在请求体上。
    assert set(inspector.urls) == {"https://example.com/api/list"}
    pages = [json.loads(body or "{}")["pageNum"] for body in inspector.bodies]
    assert pages == [1, 2, 3]
    # 原字段是数字就写回数字：服务端期望数字却收到字符串通常直接 400。
    assert all(isinstance(page, int) for page in pages)
    # 过滤条件必须原样带上，否则翻到的是另一个数据集。
    assert json.loads(inspector.bodies[1] or "{}")["status"] == "paid"


def test_nested_json_body_param_uses_dotted_path(tmp_path: Path) -> None:
    sample = json.dumps({"query": {"pageNum": 1, "pageSize": 10}, "status": "paid"})
    inspector = _json_body_inspector(tmp_path, sample_body=sample)

    full, _ = asyncio.run(
        inspector.collect_pages(
            {"exchange_id": "ex-1", "page_in": "body", "page_param": "query.pageNum"}
        )
    )

    assert full["closed"] is True
    assert full["collected"] == 25
    assert [json.loads(body or "{}")["query"]["pageNum"] for body in inspector.bodies] == [1, 2, 3]


def test_nested_json_body_param_is_guessed_from_tail_segment(tmp_path: Path) -> None:
    sample = json.dumps({"query": {"pageNum": 1, "pageSize": 10}})
    plan = build_plan(
        sample_url="https://example.com/api/list",
        analysis={},
        overrides={"page_in": "body"},
        sample_body=sample,
    )
    assert plan.param == "query.pageNum"
    assert plan.page_size == 10
    assert plan.start == 1


def test_form_encoded_body_pagination(tmp_path: Path) -> None:
    rows = [{"id": index} for index in range(12)]

    def serve(url: str, body: str | None) -> tuple[dict[str, Any], dict[str, str]]:
        fields = dict(parse_qsl(body or ""))
        page = int(fields.get("pageNo", 1))
        size = int(fields.get("size", 5))
        start = (page - 1) * size
        return {"items": rows[start : start + size], "total": 12}, {}

    log = NetworkTrafficLog(config=NetworkTrafficConfig())
    log._exchanges["ex-1"] = _exchange(
        "https://example.com/api/list",
        request_body="pageNo=1&size=5&status=paid",
        response_body=json.dumps({"items": rows[:5], "total": 12}),
    )
    inspector = _FakeInspector(log, tmp_path, serve=serve)

    full, _ = asyncio.run(inspector.collect_pages({"exchange_id": "ex-1", "page_in": "body"}))

    assert full["closed"] is True
    assert full["collected"] == 12
    assert [dict(parse_qsl(body or ""))["pageNo"] for body in inspector.bodies] == ["1", "2", "3"]
    assert dict(parse_qsl(inspector.bodies[2] or ""))["status"] == "paid"


def test_body_cursor_omits_the_field_on_the_first_page() -> None:
    sample = json.dumps({"cursor": "seed", "size": 50})
    plan = build_plan(
        sample_url="https://example.com/api/list",
        analysis={},
        overrides={"page_in": "body", "strategy": "cursor"},
        sample_body=sample,
    )
    first = json.loads(page_body(plan, sample, 0, None) or "{}")
    second = json.loads(page_body(plan, sample, 1, "tok9") or "{}")

    assert "cursor" not in first
    assert second["cursor"] == "tok9"


def test_string_page_field_keeps_its_type() -> None:
    sample = json.dumps({"pageNum": "1", "pageSize": 10})
    plan = build_plan(
        sample_url="https://example.com/api/list",
        analysis={},
        overrides={"page_in": "body"},
        sample_body=sample,
    )
    assert json.loads(page_body(plan, sample, 2, None) or "{}")["pageNum"] == "3"


def test_get_request_cannot_carry_pagination_in_the_body(tmp_path: Path) -> None:
    log = NetworkTrafficLog(config=NetworkTrafficConfig())
    log._exchanges["ex-1"] = _exchange(
        "https://example.com/api/list?page=1",
        method="GET",
        request_body=json.dumps({"pageNum": 1}),
    )
    inspector = _FakeInspector(log, tmp_path, serve=lambda url, body: ({}, {}))

    # 浏览器不会给 GET 带请求体，重放时会被丢掉，每页都会是同一页。
    with pytest.raises(ValueError, match="不能用请求体承载分页"):
        asyncio.run(inspector.collect_pages({"exchange_id": "ex-1", "page_in": "body"}))


def test_body_pagination_requires_a_parsable_body(tmp_path: Path) -> None:
    log = NetworkTrafficLog(config=NetworkTrafficConfig())
    log._exchanges["ex-1"] = _exchange("https://example.com/api/list", request_body=None)
    inspector = _FakeInspector(log, tmp_path, serve=lambda url, body: ({}, {}))

    with pytest.raises(ValueError, match="page_in=body 要求"):
        asyncio.run(inspector.collect_pages({"exchange_id": "ex-1", "page_in": "body"}))


def test_missing_json_parent_object_is_reported() -> None:
    sample = json.dumps({"pageNum": 1})
    plan = build_plan(
        sample_url="https://example.com/api/list",
        analysis={},
        overrides={"page_in": "body", "page_param": "query.pageNum", "strategy": "page_number"},
        sample_body=sample,
    )
    # 中间层不存在时明确报错，而不是凭空造出一个服务端不认的结构。
    with pytest.raises(ValueError, match="找不到分页字段"):
        page_body(plan, sample, 1, None)


# ----------------------------------------------------------------------
# 响应头游标
# ----------------------------------------------------------------------


def test_link_header_drives_the_walk(tmp_path: Path) -> None:
    pages = {
        "https://example.com/api/items?limit=2": (
            [{"id": 1}, {"id": 2}],
            '<https://example.com/api/items?after=c1&limit=2>; rel="next"',
        ),
        "https://example.com/api/items?after=c1&limit=2": (
            [{"id": 3}, {"id": 4}],
            '<https://example.com/api/items?after=c2&limit=2>; rel="next", '
            '<https://example.com/api/items?after=z&limit=2>; rel="last"',
        ),
        "https://example.com/api/items?after=c2&limit=2": ([{"id": 5}], ""),
    }

    def serve(url: str, body: str | None) -> tuple[dict[str, Any], dict[str, str]]:
        items, link = pages[url]
        return {"items": items}, ({"Link": link} if link else {})

    log = NetworkTrafficLog(config=NetworkTrafficConfig())
    sample = "https://example.com/api/items?limit=2"
    log._exchanges["ex-1"] = _exchange(
        sample, method="GET", response_body=json.dumps({"items": [{"id": 1}, {"id": 2}]})
    )
    inspector = _FakeInspector(log, tmp_path, serve=serve)

    full, _ = asyncio.run(inspector.collect_pages({"exchange_id": "ex-1", "cursor_in": "link"}))

    assert full["closed"] is True
    assert [row["id"] for row in full["records"]] == [1, 2, 3, 4, 5]
    assert full["plan"]["cursor_source"] == "link"
    # 下一页 URL 完全由服务端给出，本地不拼分页参数。
    assert full["plan"]["param"] == ""
    assert inspector.urls == list(pages)


def test_header_cursor_is_sent_back_in_the_query(tmp_path: Path) -> None:
    pages = {
        None: ([{"id": 1}, {"id": 2}], "c1"),
        "c1": ([{"id": 3}, {"id": 4}], "c2"),
        "c2": ([{"id": 5}], ""),
    }

    def serve(url: str, body: str | None) -> tuple[dict[str, Any], dict[str, str]]:
        cursor = parse_qs(urlsplit(url).query).get("cursor", [None])[0]
        items, nxt = pages[cursor]
        return {"items": items}, ({"X-Next-Cursor": nxt} if nxt else {})

    log = NetworkTrafficLog(config=NetworkTrafficConfig())
    sample = "https://example.com/api/items?cursor=&limit=2"
    log._exchanges["ex-1"] = _exchange(
        sample, method="GET", response_body=json.dumps({"items": [{"id": 1}, {"id": 2}]})
    )
    inspector = _FakeInspector(log, tmp_path, serve=serve)

    full, _ = asyncio.run(
        inspector.collect_pages(
            {
                "exchange_id": "ex-1",
                "cursor_in": "header",
                "cursor_header": "X-Next-Cursor",
                "page_param": "cursor",
            }
        )
    )

    assert full["closed"] is True
    assert [row["id"] for row in full["records"]] == [1, 2, 3, 4, 5]
    assert full["plan"]["cursor_header"] == "X-Next-Cursor"
    cursors = [parse_qs(urlsplit(url).query).get("cursor", [""])[0] for url in inspector.urls]
    assert cursors == ["", "c1", "c2"]


def test_header_cursor_requires_the_header_name() -> None:
    with pytest.raises(ValueError, match="cursor_in=header 必须提供"):
        build_plan(
            sample_url="https://example.com/api/items?cursor=a",
            analysis={},
            overrides={"cursor_in": "header"},
        )


def test_cursor_source_conflicts_with_a_non_cursor_strategy() -> None:
    with pytest.raises(ValueError, match="只在 strategy=cursor 时有意义"):
        build_plan(
            sample_url="https://example.com/api/items?page=1",
            analysis={},
            overrides={"strategy": "page_number", "cursor_in": "link"},
        )


def test_link_header_parsing_handles_multiple_relations() -> None:
    assert (
        extract_next_link(
            {"Link": '<https://a.test/1>; rel="prev", <https://a.test/2>; rel="next"'}
        )
        == "https://a.test/2"
    )
    # 不带引号的 rel 同样要认。
    assert extract_next_link({"link": "<https://a.test/3>; rel=next"}) == "https://a.test/3"
    # 只有 last/prev 时不能误认成下一页。
    assert extract_next_link({"Link": '<https://a.test/9>; rel="last"'}) is None
    assert extract_next_link({}) is None


def test_header_cursor_lookup_is_case_insensitive() -> None:
    assert extract_cursor_from_headers({"X-Next-Cursor": " tok9 "}, "x-next-cursor") == "tok9"
    assert extract_cursor_from_headers({"X-Next-Cursor": ""}, "X-Next-Cursor") is None
    assert extract_cursor_from_headers({"Other": "v"}, "X-Next-Cursor") is None

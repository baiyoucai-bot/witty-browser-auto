"""主动分页采集与闭合判定的单元测试。

重点不在"能翻页"，而在三条容易被做糊的边界：起点取值、服务端忽略分页参数、以及
"抓了一些"绝不能被包装成"抓全了"。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from witty_browser_auto.config import NetworkTrafficConfig
from witty_browser_auto.network.inspection import NetworkTrafficInspector
from witty_browser_auto.network.pagination import (
    CollectionOutcome,
    PageAttempt,
    PaginationPlan,
    build_plan,
    decide_closure,
    extract_cursor,
    extract_records,
    extract_total,
    page_url,
    record_fingerprint,
)
from witty_browser_auto.network.traffic import NetworkBody, NetworkExchange, NetworkTrafficLog


def _exchange(url: str, *, response_body: str) -> NetworkExchange:
    exchange = NetworkExchange(
        exchange_id="ex-1",
        request_id="ex-1",
        session_id="session-1",
        method="GET",
        url=url,
        status=200,
        started_wall=1000.0,
        state="finished",
    )
    exchange.request_headers = {"Cookie": "sid=abc", "Authorization": "Bearer token"}
    exchange.response_body = NetworkBody(
        text=response_body, byte_length=len(response_body.encode("utf-8"))
    )
    exchange.mime_type = "application/json"
    exchange.response_headers = {"Content-Type": "application/json"}
    return exchange


class _FakeInspector(NetworkTrafficInspector):
    """用固定数据集替换真实重放，专测遍历与闭合逻辑。"""

    def __init__(
        self,
        log: NetworkTrafficLog,
        root: Path,
        *,
        pages: Mapping[str, Any],
        failures: Mapping[int, str] | None = None,
    ) -> None:
        super().__init__(log, root, config=NetworkTrafficConfig())
        self.pages = pages
        self.failures = dict(failures or {})
        self.requested: list[str] = []

    async def replay(self, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        url = str(arguments["url"])
        self.requested.append(url)
        index = len(self.requested) - 1
        if index in self.failures:
            return {"success": False, "status": 500, "error": self.failures[index]}, {}
        return {"success": True, "status": 200, "json": self.pages(url)}, {}


def _numbered_inspector(
    tmp_path: Path,
    *,
    sample_url: str,
    total: int,
    page_size: int,
    param: str = "page",
    first_page: int = 1,
    declare_total: bool = True,
    ignore_param: bool = False,
    failures: Mapping[int, str] | None = None,
) -> _FakeInspector:
    rows = [{"id": index, "name": f"记录{index}"} for index in range(total)]

    def serve(url: str) -> dict[str, Any]:
        query = parse_qs(urlsplit(url).query)
        page = int(query.get(param, [str(first_page)])[0])
        offset = 0 if ignore_param else (page - first_page) * page_size
        payload: dict[str, Any] = {"data": {"items": rows[offset : offset + page_size]}}
        if declare_total:
            payload["total"] = total
        return payload

    log = NetworkTrafficLog(config=NetworkTrafficConfig())
    sample_body = json.dumps(serve(sample_url), ensure_ascii=False)
    log._exchanges["ex-1"] = _exchange(sample_url, response_body=sample_body)
    return _FakeInspector(log, tmp_path, pages=serve, failures=failures)


# ----------------------------------------------------------------------
# 计划推断
# ----------------------------------------------------------------------


def test_plan_takes_its_start_from_the_sample_url() -> None:
    # 有的接口页码从 0 起，有的从 1 起；猜错会漏首页或多抓一页。
    zero_based = build_plan(
        sample_url="https://example.com/api?page=0&size=20",
        analysis={"pagination": {"strategy": "page_number"}},
        overrides={},
    )
    assert zero_based.start == 0
    one_based = build_plan(
        sample_url="https://example.com/api?page=3&size=20",
        analysis={"pagination": {"strategy": "page_number"}},
        overrides={},
    )
    assert one_based.start == 3
    assert one_based.page_size == 20


def test_plan_falls_back_to_query_inspection_when_analysis_says_none() -> None:
    plan = build_plan(
        sample_url="https://example.com/api?offset=40&limit=20",
        analysis={"pagination": {"strategy": "none"}},
        overrides={},
    )
    assert plan.strategy == "offset"
    assert plan.param == "offset"
    # offset 每页前进一整页，不是加一。
    assert plan.step == 20


def test_plan_refuses_to_guess_when_there_is_nothing_to_go_on() -> None:
    with pytest.raises(ValueError, match="无法确定分页策略"):
        build_plan(
            sample_url="https://example.com/api?keyword=x",
            analysis={},
            overrides={},
        )


def test_offset_strategy_requires_a_known_page_size() -> None:
    with pytest.raises(ValueError, match="每页大小"):
        build_plan(
            sample_url="https://example.com/api?offset=0",
            analysis={"pagination": {"strategy": "offset"}},
            overrides={},
        )


def test_explicit_overrides_beat_inference() -> None:
    plan = build_plan(
        sample_url="https://example.com/api?p=2&n=10",
        analysis={"pagination": {"strategy": "none"}},
        overrides={
            "strategy": "page_number",
            "page_param": "p",
            "start": 1,
            "page_size": 10,
            "record_path": ["result", "rows"],
        },
    )
    assert (plan.strategy, plan.param, plan.start) == ("page_number", "p", 1)
    assert plan.record_path == ("result", "rows")


def test_page_url_rewrites_only_the_pagination_parameter() -> None:
    plan = build_plan(
        sample_url="https://example.com/api?page=1&size=20&status=paid",
        analysis={"pagination": {"strategy": "page_number"}},
        overrides={},
    )
    third = page_url(plan, "https://example.com/api?page=1&size=20&status=paid", 2, None)
    query = parse_qs(urlsplit(third).query)
    assert query["page"] == ["3"]
    # 过滤条件必须原样带上，否则翻到的是另一个数据集。
    assert query["status"] == ["paid"]
    assert query["size"] == ["20"]


def test_cursor_first_page_drops_the_cursor_parameter() -> None:
    plan = build_plan(
        sample_url="https://example.com/api?cursor=abc&limit=10",
        analysis={"pagination": {"strategy": "cursor"}},
        overrides={},
    )
    first = page_url(plan, "https://example.com/api?cursor=abc&limit=10", 0, None)
    assert "cursor" not in parse_qs(urlsplit(first).query)
    second = page_url(plan, "https://example.com/api?cursor=abc&limit=10", 1, "next-token")
    assert parse_qs(urlsplit(second).query)["cursor"] == ["next-token"]


# ----------------------------------------------------------------------
# 响应解析
# ----------------------------------------------------------------------


def test_records_and_total_are_read_from_their_declared_places() -> None:
    payload = {"data": {"items": [{"id": 1}, {"id": 2}]}, "total": 87}
    assert len(extract_records(payload, ["data", "items"])) == 2
    assert extract_records(payload, ["data", "missing"]) == []
    assert extract_total(payload) == 87
    assert extract_total(payload, ["data", "items"]) is None


def test_total_is_none_rather_than_a_guess_when_undeclared() -> None:
    # 拿收集数冒充总数会让完整性门形同虚设。
    assert extract_total({"data": {"items": []}}) is None


def test_cursor_is_found_in_nested_paging_objects() -> None:
    assert extract_cursor({"next_cursor": "abc"}, None) == "abc"
    assert extract_cursor({"paging": {"next": "def"}}, None) == "def"
    assert extract_cursor({"pageInfo": {"nextCursor": "ghi"}}, None) == "ghi"
    assert extract_cursor({"data": []}, None) is None


def test_fingerprint_prefers_the_declared_key_but_falls_back_to_content() -> None:
    left = {"id": 7, "updated": "早"}
    right = {"id": 7, "updated": "晚"}
    assert record_fingerprint(left, "id") == record_fingerprint(right, "id")
    assert record_fingerprint(left, None) != record_fingerprint(right, None)


# ----------------------------------------------------------------------
# 遍历与闭合
# ----------------------------------------------------------------------


def test_page_number_walk_collects_every_record_and_closes(tmp_path: Path) -> None:
    inspector = _numbered_inspector(
        tmp_path, sample_url="https://example.com/api?page=1&size=20", total=87, page_size=20
    )
    full, model = asyncio.run(inspector.collect_pages({"exchange_id": "ex-1"}))

    assert full["closed"] is True
    assert full["collected"] == 87
    assert full["declared_total"] == 87
    assert len(full["records"]) == 87
    assert [row["id"] for row in full["records"]] == list(range(87))
    # 87 条按每页 20 条正好落在第 5 页。
    assert full["pages_fetched"] == 5
    assert model["collected"] == 87
    # 业务数据只回给调用方，模型侧只见计数与结论。
    assert "records" not in model


def test_zero_based_pages_do_not_lose_the_first_page(tmp_path: Path) -> None:
    inspector = _numbered_inspector(
        tmp_path,
        sample_url="https://example.com/api?page=0&size=10",
        total=25,
        page_size=10,
        first_page=0,
    )
    full, _ = asyncio.run(inspector.collect_pages({"exchange_id": "ex-1"}))

    assert full["closed"] is True
    assert [row["id"] for row in full["records"]] == list(range(25))


def test_offset_walk_advances_by_a_whole_page(tmp_path: Path) -> None:
    rows = [{"id": index} for index in range(45)]

    def serve(url: str) -> dict[str, Any]:
        query = parse_qs(urlsplit(url).query)
        offset = int(query.get("offset", ["0"])[0])
        limit = int(query.get("limit", ["15"])[0])
        return {"items": rows[offset : offset + limit], "total": 45}

    log = NetworkTrafficLog(config=NetworkTrafficConfig())
    sample = "https://example.com/api?offset=0&limit=15"
    log._exchanges["ex-1"] = _exchange(sample, response_body=json.dumps(serve(sample)))
    inspector = _FakeInspector(log, tmp_path, pages=serve)

    full, _ = asyncio.run(inspector.collect_pages({"exchange_id": "ex-1"}))

    assert full["closed"] is True
    assert full["collected"] == 45
    offsets = [parse_qs(urlsplit(url).query)["offset"][0] for url in inspector.requested]
    assert offsets == ["0", "15", "30"]


def test_cursor_walk_stops_when_the_server_stops_handing_out_cursors(tmp_path: Path) -> None:
    pages = {
        None: {"items": [{"id": 1}, {"id": 2}], "next_cursor": "c1"},
        "c1": {"items": [{"id": 3}, {"id": 4}], "next_cursor": "c2"},
        "c2": {"items": [{"id": 5}], "next_cursor": None},
    }

    def serve(url: str) -> dict[str, Any]:
        query = parse_qs(urlsplit(url).query)
        cursor = query.get("cursor", [None])[0]
        return pages[cursor]

    log = NetworkTrafficLog(config=NetworkTrafficConfig())
    sample = "https://example.com/api?cursor=&limit=2"
    log._exchanges["ex-1"] = _exchange(sample, response_body=json.dumps(pages[None]))
    inspector = _FakeInspector(log, tmp_path, pages=serve)

    full, _ = asyncio.run(
        inspector.collect_pages(
            {"exchange_id": "ex-1", "strategy": "cursor", "page_param": "cursor"}
        )
    )

    assert full["closed"] is True
    assert [row["id"] for row in full["records"]] == [1, 2, 3, 4, 5]
    assert "末页" in full["reason"]


def test_server_ignoring_the_pagination_parameter_is_caught(tmp_path: Path) -> None:
    # 参数名猜错时服务端照返第一页；天真的循环会一直抓到页数上限还自认为成功。
    inspector = _numbered_inspector(
        tmp_path,
        sample_url="https://example.com/api?page=1&size=20",
        total=87,
        page_size=20,
        ignore_param=True,
    )
    full, _ = asyncio.run(inspector.collect_pages({"exchange_id": "ex-1"}))

    assert full["closed"] is False
    assert "忽略了分页参数" in full["failed_pages"][0]["error"]
    # 第二页就该发现，不该把 50 页额度耗光。
    assert full["pages_fetched"] == 2


def test_a_short_collection_is_never_reported_as_complete(tmp_path: Path) -> None:
    inspector = _numbered_inspector(
        tmp_path,
        sample_url="https://example.com/api?page=1&size=20",
        total=87,
        page_size=20,
    )
    full, _ = asyncio.run(inspector.collect_pages({"exchange_id": "ex-1", "max_pages": 2}))

    assert full["closed"] is False
    assert full["collected"] == 40
    assert full["declared_total"] == 87
    assert "相差 47 条" in full["reason"]


def test_hitting_the_page_budget_without_a_declared_total_is_not_closure(tmp_path: Path) -> None:
    inspector = _numbered_inspector(
        tmp_path,
        sample_url="https://example.com/api?page=1&size=20",
        total=200,
        page_size=20,
        declare_total=False,
    )
    full, _ = asyncio.run(inspector.collect_pages({"exchange_id": "ex-1", "max_pages": 3}))

    assert full["closed"] is False
    assert full["collected"] == 60
    assert "页数上限" in full["reason"]


def test_a_failed_page_blocks_closure_and_names_the_page(tmp_path: Path) -> None:
    inspector = _numbered_inspector(
        tmp_path,
        sample_url="https://example.com/api?page=1&size=20",
        total=87,
        page_size=20,
        failures={2: "网关超时"},
    )
    full, _ = asyncio.run(inspector.collect_pages({"exchange_id": "ex-1"}))

    assert full["closed"] is False
    assert "第 2 页请求失败" in full["reason"]
    assert full["failed_pages"][0]["index"] == 2
    # 失败前抓到的两页仍然保留，便于调用方续采。
    assert full["collected"] == 40


def test_duplicate_records_across_pages_are_merged(tmp_path: Path) -> None:
    # 边翻页边有新数据插入时，相邻页会重叠；重复计数会让完整性门误判。
    rows = [{"id": index} for index in range(10)]
    served: list[list[dict[str, Any]]] = [rows[0:4], rows[3:7], rows[6:10], []]

    def serve(url: str) -> dict[str, Any]:
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        return {"items": served[min(page - 1, len(served) - 1)]}

    log = NetworkTrafficLog(config=NetworkTrafficConfig())
    sample = "https://example.com/api?page=1"
    log._exchanges["ex-1"] = _exchange(sample, response_body=json.dumps(serve(sample)))
    inspector = _FakeInspector(log, tmp_path, pages=serve)

    full, _ = asyncio.run(
        inspector.collect_pages({"exchange_id": "ex-1", "dedupe_key": "id", "max_pages": 4})
    )

    assert full["closed"] is True
    assert [row["id"] for row in full["records"]] == list(range(10))
    assert full["pages"][1]["records"] == 4
    assert full["pages"][1]["new_records"] == 3


def test_page_budget_bounds_are_enforced(tmp_path: Path) -> None:
    inspector = _numbered_inspector(
        tmp_path, sample_url="https://example.com/api?page=1&size=20", total=20, page_size=20
    )
    with pytest.raises(ValueError, match="max_pages"):
        asyncio.run(inspector.collect_pages({"exchange_id": "ex-1", "max_pages": 1000}))


def test_non_json_response_stops_the_walk(tmp_path: Path) -> None:
    log = NetworkTrafficLog(config=NetworkTrafficConfig())
    sample = "https://example.com/api?page=1&size=20"
    log._exchanges["ex-1"] = _exchange(sample, response_body='{"items": [], "total": 5}')

    class _HtmlInspector(_FakeInspector):
        async def replay(
            self, arguments: Mapping[str, Any]
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            return {"success": True, "status": 200}, {}

    inspector = _HtmlInspector(log, tmp_path, pages=lambda url: {})
    full, _ = asyncio.run(inspector.collect_pages({"exchange_id": "ex-1"}))

    assert full["closed"] is False
    assert "不是 JSON" in full["failed_pages"][0]["error"]


# ----------------------------------------------------------------------
# 闭合判据本身
# ----------------------------------------------------------------------


def _outcome(records: int, pages: list[PageAttempt], total: int | None) -> CollectionOutcome:
    plan = PaginationPlan(
        strategy="page_number",
        param="page",
        start=1,
        step=1,
        page_size=20,
        record_path=("items",),
    )
    return CollectionOutcome(
        plan=plan,
        records=[{"id": index} for index in range(records)],
        pages=pages,
        declared_total=total,
    )


def test_closure_needs_positive_evidence_not_just_a_quiet_finish() -> None:
    # 没有声明总数、末页又是满页，只能说明"还没证据"，不能算取全。
    outcome = _outcome(40, [PageAttempt(0, "u", records=20), PageAttempt(1, "u", records=20)], None)
    decide_closure(outcome, exhausted_budget=False)
    assert outcome.closed is False
    assert "无法确认取全" in outcome.reason


def test_a_short_last_page_closes_without_a_declared_total() -> None:
    outcome = _outcome(35, [PageAttempt(0, "u", records=20), PageAttempt(1, "u", records=15)], None)
    decide_closure(outcome, exhausted_budget=False)
    assert outcome.closed is True
    assert "少于每页" in outcome.reason

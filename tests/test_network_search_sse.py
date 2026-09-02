"""流量全文搜索与 SSE 消息读取的单元测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from witty_browser_auto.agent import traffic_tools
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.config import NetworkTrafficConfig
from witty_browser_auto.network.inspection import NetworkTrafficInspector
from witty_browser_auto.network.traffic import (
    NetworkBody,
    NetworkExchange,
    NetworkTrafficLog,
    ServerSentEvent,
)

SESSION = "s-1"


def _event(method: str, params: dict[str, Any]) -> CdpEvent:
    return CdpEvent(method=method, params=params, session_id=SESSION)


# ---------------------------------------------------------------------------
# 全文搜索
# ---------------------------------------------------------------------------


def _body(text: str) -> NetworkBody:
    return NetworkBody(text=text, byte_length=len(text.encode("utf-8")))


def _search_setup(tmp_path: Path) -> NetworkTrafficInspector:
    config = NetworkTrafficConfig()
    log = NetworkTrafficLog(config)

    orders = NetworkExchange(
        exchange_id="ex-1",
        request_id="req-1",
        session_id=SESSION,
        method="GET",
        url="https://shop.test/api/orders?page=1",
        resource_type="XHR",
        status=200,
    )
    orders.response_body = _body('{"list": [{"order_no": "SO-8899", "amount": 128.0}]}')
    orders.request_headers = {"Authorization": "Bearer tok-abcdef"}

    profile = NetworkExchange(
        exchange_id="ex-2",
        request_id="req-2",
        session_id=SESSION,
        method="POST",
        url="https://shop.test/api/profile",
        resource_type="Fetch",
        status=200,
    )
    profile.request_body = _body('{"keyword": "SO-8899 lookup"}')
    profile.response_body = _body('{"name": "\u5f20\u4e09"}')

    for exchange in (orders, profile):
        log._exchanges[exchange.exchange_id] = exchange
    return NetworkTrafficInspector(log, tmp_path, config=config)


def test_search_body_finds_value_and_returns_snippet(tmp_path: Path) -> None:
    inspector = _search_setup(tmp_path)
    full, _ = asyncio.run(inspector.search({"query": "SO-8899"}))

    # 默认 body 范围覆盖请求体与响应体，两条交换都命中。
    assert full["returned_count"] == 2
    ids = {match["exchange_id"] for match in full["matches"]}
    assert ids == {"ex-1", "ex-2"}
    order_match = next(m for m in full["matches"] if m["exchange_id"] == "ex-1")
    assert order_match["part"] == "response_body"
    assert "SO-8899" in order_match["snippet"]
    assert order_match["match_count"] == 1


def test_search_headers_scope_locates_token_carrier(tmp_path: Path) -> None:
    inspector = _search_setup(tmp_path)
    full, _ = asyncio.run(inspector.search({"query": "tok-abcdef", "scope": "headers"}))

    assert full["returned_count"] == 1
    match = full["matches"][0]
    assert match["exchange_id"] == "ex-1"
    assert match["part"] == "request_header"
    assert match["field_name"] == "Authorization"


def test_search_body_scope_excludes_header_hits(tmp_path: Path) -> None:
    inspector = _search_setup(tmp_path)
    # token 只在 Header 里，默认 body 范围搜不到。
    full, _ = asyncio.run(inspector.search({"query": "tok-abcdef"}))
    assert full["returned_count"] == 0


def test_search_url_contains_filters_before_scanning(tmp_path: Path) -> None:
    inspector = _search_setup(tmp_path)
    full, _ = asyncio.run(inspector.search({"query": "SO-8899", "url_contains": "/api/orders"}))
    assert full["returned_count"] == 1
    assert full["matches"][0]["exchange_id"] == "ex-1"


def test_search_is_case_insensitive_by_default(tmp_path: Path) -> None:
    inspector = _search_setup(tmp_path)
    insensitive, _ = asyncio.run(inspector.search({"query": "so-8899"}))
    assert insensitive["returned_count"] == 2
    sensitive, _ = asyncio.run(inspector.search({"query": "so-8899", "case_sensitive": True}))
    assert sensitive["returned_count"] == 0


def test_search_model_view_hides_snippets(tmp_path: Path) -> None:
    inspector = _search_setup(tmp_path)
    _, model = asyncio.run(inspector.search({"query": "SO-8899"}))
    serialized = json.dumps(model, ensure_ascii=False)
    assert "snippet" not in serialized
    assert "128.0" not in serialized
    # 交换定位与命中次数必须保留，否则模型无法接着 read_network_body。
    assert model["matches"][0]["exchange_id"] in {"ex-1", "ex-2"}
    assert "match_count" in model["matches"][0]


def test_search_unsupported_scope_is_rejected(tmp_path: Path) -> None:
    inspector = _search_setup(tmp_path)
    with pytest.raises(ValueError, match="不支持的搜索范围"):
        asyncio.run(inspector.search({"query": "x", "scope": "cookies"}))


def test_search_requires_query(tmp_path: Path) -> None:
    inspector = _search_setup(tmp_path)
    with pytest.raises(ValueError, match="必须提供 query"):
        asyncio.run(inspector.search({}))


def test_search_tool_dispatch_reports_success_flag(tmp_path: Path) -> None:
    inspector = _search_setup(tmp_path)
    hit = asyncio.run(
        traffic_tools.execute_traffic_tool(
            "search_network_traffic", {"query": "SO-8899"}, inspector, task_inputs={}
        )
    )
    assert hit.success is True
    miss = asyncio.run(
        traffic_tools.execute_traffic_tool(
            "search_network_traffic", {"query": "nonexistent-xyz"}, inspector, task_inputs={}
        )
    )
    assert miss.success is False
    with pytest.raises(ValueError, match="未知参数"):
        asyncio.run(
            traffic_tools.execute_traffic_tool(
                "search_network_traffic",
                {"query": "x", "bogus": 1},
                inspector,
                task_inputs={},
            )
        )


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def _open_sse_request(log: NetworkTrafficLog, request_id: str = "sse-1") -> None:
    log.on_request(
        _event(
            "Network.requestWillBeSent",
            {
                "requestId": request_id,
                "request": {
                    "url": "https://llm.test/v1/stream",
                    "method": "GET",
                    "headers": {"Accept": "text/event-stream"},
                },
                "type": "EventSource",
                "timestamp": 1000.0,
                "wallTime": 1_700_000_000.0,
            },
        )
    )


def _sse_message(
    request_id: str, data: str, *, event: str = "message", event_id: str = ""
) -> CdpEvent:
    return _event(
        "Network.eventSourceMessageReceived",
        {
            "requestId": request_id,
            "timestamp": 1001.0,
            "eventName": event,
            "eventId": event_id,
            "data": data,
        },
    )


def test_sse_messages_are_recorded_on_the_exchange(tmp_path: Path) -> None:
    config = NetworkTrafficConfig()
    log = NetworkTrafficLog(config)
    _open_sse_request(log)
    log.on_event_source_message(_sse_message("sse-1", '{"delta": "你"}'))
    log.on_event_source_message(_sse_message("sse-1", '{"delta": "好"}', event="chunk"))

    exchange = log.ordered()[0]
    assert exchange.is_event_source is True
    assert exchange.resource_type == "EventSource"
    assert len(exchange.sse_messages) == 2
    # SSE 交换在 full_dict 里有专门的 event_source 块，model_dict 只给计数。
    assert exchange.full_dict()["event_source"]["message_count"] == 2
    assert exchange.model_dict()["sse_message_count"] == 2


def test_sse_message_bytes_are_truncated_to_limit() -> None:
    config = NetworkTrafficConfig(max_websocket_frame_bytes=256)
    log = NetworkTrafficLog(config)
    _open_sse_request(log)
    log.on_event_source_message(_sse_message("sse-1", "x" * 300))
    message = log.ordered()[0].sse_messages[0]
    assert message.truncated is True
    assert message.byte_length == 300
    assert len(message.data.encode("utf-8")) <= 256


def _sse_inspector(tmp_path: Path) -> NetworkTrafficInspector:
    config = NetworkTrafficConfig()
    log = NetworkTrafficLog(config)
    exchange = NetworkExchange(
        exchange_id="ex-1",
        request_id="sse-1",
        session_id=SESSION,
        url="https://llm.test/v1/stream?token=secret999",
        resource_type="EventSource",
        state="open",
    )
    exchange.is_event_source = True
    exchange.sse_messages = [
        ServerSentEvent("message", '{"delta": "hello"}', "1", 18, False, 1.0),
        ServerSentEvent("chunk", '{"delta": "world"}', "2", 18, False, 2.0),
        ServerSentEvent("chunk", "not json", "3", 8, False, 3.0),
        ServerSentEvent("done", "[DONE]", "4", 6, False, 4.0),
    ]
    log._exchanges["ex-1"] = exchange
    return NetworkTrafficInspector(log, tmp_path, config=config)


def test_read_sse_returns_messages_with_parsed_json(tmp_path: Path) -> None:
    inspector = _sse_inspector(tmp_path)
    full, _ = asyncio.run(inspector.read_sse({"exchange_id": "ex-1"}))
    assert full["returned_count"] == 4
    assert full["message_count"] == 4
    assert full["events"] == {"message": 1, "chunk": 2, "done": 1}
    assert full["messages"][0]["json"] == {"delta": "hello"}
    assert "json" not in full["messages"][2]


def test_read_sse_filters_by_event_name_and_substring(tmp_path: Path) -> None:
    inspector = _sse_inspector(tmp_path)
    by_event, _ = asyncio.run(inspector.read_sse({"exchange_id": "ex-1", "event_name": "chunk"}))
    assert by_event["returned_count"] == 2
    assert by_event["matched_count"] == 2
    by_text, _ = asyncio.run(inspector.read_sse({"exchange_id": "ex-1", "contains": "world"}))
    assert by_text["returned_count"] == 1
    assert by_text["messages"][0]["data"] == '{"delta": "world"}'
    # 统计始终针对整条连接。
    assert by_text["message_count"] == 4


def test_read_sse_limit_keeps_newest(tmp_path: Path) -> None:
    inspector = _sse_inspector(tmp_path)
    full, _ = asyncio.run(inspector.read_sse({"exchange_id": "ex-1", "limit": 1}))
    assert full["returned_count"] == 1
    assert full["messages"][0]["data"] == "[DONE]"


def test_read_sse_model_view_hides_message_content(tmp_path: Path) -> None:
    inspector = _sse_inspector(tmp_path)
    _, model = asyncio.run(inspector.read_sse({"exchange_id": "ex-1"}))
    serialized = json.dumps(model, ensure_ascii=False)
    assert "messages" not in model
    assert "hello" not in serialized
    assert "secret999" not in serialized
    assert model["message_count"] == 4


def test_read_sse_rejects_non_sse_exchange(tmp_path: Path) -> None:
    config = NetworkTrafficConfig()
    log = NetworkTrafficLog(config)
    log._exchanges["ex-1"] = NetworkExchange(
        exchange_id="ex-1", request_id="r", session_id=SESSION, url="https://x.test/"
    )
    inspector = NetworkTrafficInspector(log, tmp_path, config=config)
    with pytest.raises(ValueError, match="不是 SSE 连接"):
        asyncio.run(inspector.read_sse({"exchange_id": "ex-1"}))

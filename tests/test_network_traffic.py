"""流量日志、HAR 导出与流量工具的单元测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from witty_browser_auto.agent import traffic_tools
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.config import NetworkTrafficConfig
from witty_browser_auto.network.har import build_har
from witty_browser_auto.network.inspection import NetworkTrafficInspector
from witty_browser_auto.network.traffic import NetworkTiming, NetworkTrafficLog

SESSION = "session-1"


class FakeBodySession:
    """只回应正文读取命令的假会话。"""

    def __init__(self, bodies: dict[str, tuple[str, bool]] | None = None) -> None:
        self.bodies = bodies or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        self.calls.append((method, params))
        if method == "Network.getResponseBody":
            body = self.bodies.get(params["requestId"])
            if body is None:
                raise RuntimeError("No resource with given identifier found")
            return {"body": body[0], "base64Encoded": body[1]}
        if method == "Network.getRequestPostData":
            return {"postData": self.bodies.get(f"post:{params['requestId']}", ("", False))[0]}
        raise AssertionError(f"未预期的 CDP 命令：{method}")

    @property
    def body_reads(self) -> list[dict[str, Any]]:
        return [params for method, params in self.calls if method == "Network.getResponseBody"]


def _event(method: str, params: dict[str, Any]) -> CdpEvent:
    return CdpEvent(method=method, params=params, session_id=SESSION)


def _request_event(
    request_id: str = "req-1",
    *,
    url: str = "https://example.com/api/orders?page=1",
    method: str = "POST",
    headers: dict[str, str] | None = None,
    post_data: str | None = None,
    resource_type: str = "XHR",
    redirect_response: dict[str, Any] | None = None,
) -> CdpEvent:
    request: dict[str, Any] = {
        "url": url,
        "method": method,
        "headers": headers if headers is not None else {"Accept": "application/json"},
    }
    if post_data is not None:
        request["postData"] = post_data
        request["hasPostData"] = True
    params: dict[str, Any] = {
        "requestId": request_id,
        "request": request,
        "type": resource_type,
        "frameId": "frame-1",
        "timestamp": 1000.0,
        "wallTime": 1_700_000_000.0,
        "initiator": {
            "type": "script",
            "url": "https://example.com/app.js",
            "stack": {
                "callFrames": [
                    {
                        "functionName": "loadOrders",
                        "url": "https://example.com/app.js",
                        "lineNumber": 42,
                    }
                ]
            },
        },
    }
    if redirect_response is not None:
        params["redirectResponse"] = redirect_response
    return _event("Network.requestWillBeSent", params)


def _response_event(
    request_id: str = "req-1",
    *,
    status: int = 200,
    resource_type: str = "XHR",
) -> CdpEvent:
    return _event(
        "Network.responseReceived",
        {
            "requestId": request_id,
            "type": resource_type,
            "response": {
                "url": "https://example.com/api/orders?page=1",
                "status": status,
                "statusText": "OK",
                "headers": {"Content-Type": "application/json", "Set-Cookie": "a=1"},
                "mimeType": "application/json",
                "protocol": "h2",
                "remoteIPAddress": "93.184.216.34",
                "remotePort": 443,
                "securityState": "secure",
                "timing": {
                    "requestTime": 1000.0,
                    "dnsStart": 1.0,
                    "dnsEnd": 6.0,
                    "connectStart": 6.0,
                    "connectEnd": 20.0,
                    "sslStart": 10.0,
                    "sslEnd": 20.0,
                    "sendStart": 20.0,
                    "sendEnd": 22.0,
                    "receiveHeadersEnd": 60.0,
                },
            },
        },
    )


def _finished_event(request_id: str = "req-1", *, encoded: int = 512) -> CdpEvent:
    return _event(
        "Network.loadingFinished",
        {"requestId": request_id, "timestamp": 1000.08, "encodedDataLength": encoded},
    )


def _log(**overrides: Any) -> NetworkTrafficLog:
    return NetworkTrafficLog(NetworkTrafficConfig(**overrides))


async def _record_one(
    log: NetworkTrafficLog,
    session: FakeBodySession,
    **request_kwargs: Any,
) -> None:
    log.on_request(_request_event(**request_kwargs))
    log.on_response(_response_event())
    await log.on_finished(session, _finished_event())


def _inspector(log: NetworkTrafficLog, artifact_root: Path) -> NetworkTrafficInspector:
    return NetworkTrafficInspector(
        log,
        artifact_root,
        config=log.config,
        allowed_origins=("https://example.com",),
    )


def test_log_records_headers_timing_initiator_and_body() -> None:
    log = _log()
    session = FakeBodySession({"req-1": ('{"total": 3}', False)})

    async def scenario() -> None:
        await _record_one(log, session, post_data='{"page":1}')

    asyncio.run(scenario())

    (exchange,) = log.ordered()
    payload = exchange.full_dict()
    assert payload["method"] == "POST"
    assert payload["url"] == "https://example.com/api/orders?page=1"
    assert payload["status"] == 200
    assert payload["protocol"] == "h2"
    assert payload["remote_address"] == "93.184.216.34"
    assert payload["request_headers"]["Accept"] == "application/json"
    assert payload["response_headers"]["Set-Cookie"] == "a=1"
    assert payload["initiator"]["type"] == "script"
    assert payload["initiator"]["call_frames"][0]["function_name"] == "loadOrders"
    timing = payload["timing"]
    assert timing["dns_ms"] == 5.0
    assert timing["connect_ms"] == 14.0
    assert timing["ssl_ms"] == 10.0
    assert timing["send_ms"] == 2.0
    assert timing["wait_ms"] == 38.0
    assert timing["total_ms"] == pytest.approx(80.0, abs=0.01)
    assert timing["receive_ms"] == pytest.approx(20.0, abs=0.01)
    assert exchange.response_body is not None
    assert exchange.response_body.text == '{"total": 3}'
    assert exchange.request_body is not None
    assert exchange.request_body.text == '{"page":1}'


def test_model_view_hides_header_values_and_bodies() -> None:
    log = _log()
    session = FakeBodySession({"req-1": ('{"secret": "abc"}', False)})

    async def scenario() -> None:
        await _record_one(log, session)

    asyncio.run(scenario())

    (exchange,) = log.ordered()
    model = exchange.model_dict()
    serialized = json.dumps(model, ensure_ascii=False)
    assert "abc" not in serialized
    assert "a=1" not in serialized
    assert model["request_header_names"] == ["Accept"]
    assert "Set-Cookie" in model["response_header_names"]
    assert model["response_body_bytes"] == len('{"secret": "abc"}')


def test_extra_info_arriving_before_request_is_not_lost() -> None:
    """CDP 可能先送 requestWillBeSentExtraInfo，晚到的交换必须能补上这批 Header。"""

    log = _log()
    log.on_request_extra_info(
        _event(
            "Network.requestWillBeSentExtraInfo",
            {"requestId": "req-1", "headers": {"Cookie": "session=xyz"}},
        )
    )
    session = FakeBodySession({"req-1": ("{}", False)})

    async def scenario() -> None:
        await _record_one(log, session)

    asyncio.run(scenario())

    (exchange,) = log.ordered()
    assert exchange.request_headers["Cookie"] == "session=xyz"


def test_redirect_closes_previous_exchange_and_opens_a_new_one() -> None:
    log = _log()
    session = FakeBodySession({"req-1": ("{}", False)})

    async def scenario() -> None:
        log.on_request(_request_event(url="https://example.com/old", method="GET"))
        log.on_request(
            _request_event(
                url="https://example.com/new",
                method="GET",
                redirect_response={
                    "url": "https://example.com/old",
                    "status": 302,
                    "headers": {"Location": "/new"},
                },
            )
        )
        log.on_response(_response_event())
        await log.on_finished(session, _finished_event())

    asyncio.run(scenario())

    first, second = log.ordered()
    assert first.status == 302
    assert first.redirect_to == "https://example.com/new"
    assert first.state == "finished"
    assert second.url == "https://example.com/new"
    assert first.exchange_id != second.exchange_id


def test_resource_type_outside_capture_range_skips_body_read() -> None:
    log = _log(body_resource_types=("XHR",))
    session = FakeBodySession({"req-1": ("binary", True)})

    async def scenario() -> None:
        log.on_request(_request_event(resource_type="Image"))
        log.on_response(_response_event(resource_type="Image"))
        await log.on_finished(session, _finished_event())

    asyncio.run(scenario())

    (exchange,) = log.ordered()
    assert exchange.response_body is not None
    assert exchange.response_body.available is False
    assert "资源类型" in exchange.response_body.reason
    assert session.body_reads == []


def test_body_over_single_limit_records_length_only() -> None:
    log = _log(max_body_bytes=1024, max_total_body_bytes=4096)
    session = FakeBodySession({"req-1": ("x" * 5000, False)})

    async def scenario() -> None:
        log.on_request(_request_event())
        log.on_response(_response_event())
        await log.on_finished(session, _finished_event(encoded=5000))

    asyncio.run(scenario())

    (exchange,) = log.ordered()
    assert exchange.response_body is not None
    assert exchange.response_body.available is False
    assert exchange.response_body.byte_length == 5000
    assert session.body_reads == []


def test_global_body_budget_releases_oldest_bodies() -> None:
    log = _log(max_body_bytes=1024, max_total_body_bytes=1500)

    async def scenario() -> None:
        for index in range(3):
            request_id = f"req-{index}"
            session = FakeBodySession({request_id: ("y" * 800, False)})
            log.on_request(_request_event(request_id))
            log.on_response(_response_event(request_id))
            await log.on_finished(session, _finished_event(request_id, encoded=800))

    asyncio.run(scenario())

    bodies = [item.response_body for item in log.ordered()]
    assert bodies[0] is not None and bodies[0].available is False
    assert "预算" in bodies[0].reason
    assert bodies[-1] is not None and bodies[-1].available is True
    assert log.stats()["buffered_body_bytes"] <= 1500


def test_failed_request_keeps_reason_and_marks_state() -> None:
    log = _log()
    log.on_request(_request_event())
    log.on_failed(
        _event(
            "Network.loadingFailed",
            {
                "requestId": "req-1",
                "timestamp": 1000.02,
                "errorText": "net::ERR_CONNECTION_REFUSED",
                "blockedReason": "other",
                "corsErrorStatus": {"corsError": "MissingAllowOriginHeader"},
            },
        )
    )
    (exchange,) = log.ordered()
    assert exchange.state == "failed"
    assert exchange.error_text == "net::ERR_CONNECTION_REFUSED"
    assert exchange.blocked_reason == "other"
    assert exchange.cors_error == "MissingAllowOriginHeader"
    assert exchange.timing.total_ms == pytest.approx(20.0, abs=0.01)


def test_websocket_frames_are_recorded_with_direction_and_truncation() -> None:
    log = _log(max_websocket_frame_bytes=256)
    log.on_websocket_created(
        _event("Network.webSocketCreated", {"requestId": "ws-1", "url": "wss://example.com/live"})
    )
    log.on_websocket_handshake_response(
        _event(
            "Network.webSocketHandshakeResponseReceived",
            {
                "requestId": "ws-1",
                "response": {
                    "status": 101,
                    "statusText": "Switching Protocols",
                    "headers": {"Upgrade": "websocket"},
                },
            },
        )
    )
    log.on_websocket_frame(
        _event(
            "Network.webSocketFrameSent",
            {"requestId": "ws-1", "timestamp": 1.0, "response": {"opcode": 1, "payloadData": "hi"}},
        ),
        direction="sent",
    )
    log.on_websocket_frame(
        _event(
            "Network.webSocketFrameReceived",
            {
                "requestId": "ws-1",
                "timestamp": 2.0,
                "response": {"opcode": 1, "payloadData": "z" * 400},
            },
        ),
        direction="received",
    )
    log.on_websocket_closed(_event("Network.webSocketClosed", {"requestId": "ws-1"}))

    (exchange,) = log.ordered()
    assert exchange.is_websocket is True
    assert exchange.status == 101
    assert exchange.state == "finished"
    sent, received = exchange.websocket_frames
    assert (sent.direction, sent.opcode, sent.payload) == ("sent", "text", "hi")
    assert received.direction == "received"
    assert received.truncated is True
    assert received.byte_length == 400
    assert len(received.payload.encode("utf-8")) == 256


def test_select_filters_by_url_method_status_and_failure() -> None:
    log = _log()
    session = FakeBodySession({"req-1": ("{}", False)})

    async def scenario() -> None:
        await _record_one(log, session)
        log.on_request(
            _request_event(
                "req-2",
                url="https://example.com/static/app.css",
                method="GET",
                resource_type="Stylesheet",
            )
        )
        log.on_failed(
            _event("Network.loadingFailed", {"requestId": "req-2", "errorText": "net::ERR_ABORTED"})
        )

    asyncio.run(scenario())

    assert len(log.select(url_contains="/api/")) == 1
    assert len(log.select(methods=["GET"])) == 1
    assert len(log.select(status_min=200, status_max=299)) == 1
    assert len(log.select(only_failed=True)) == 1
    assert len(log.select(resource_types=["xhr"])) == 1
    assert len(log.select(limit=1)) == 1


def test_max_exchanges_evicts_oldest() -> None:
    log = _log(max_exchanges=10)
    for index in range(15):
        log.on_request(_request_event(f"req-{index}", url=f"https://example.com/{index}"))
    assert log.stats()["exchange_count"] == 10
    assert log.ordered()[0].url.endswith("/5")


def test_timing_marks_unavailable_stages_as_none() -> None:
    timing = NetworkTiming.from_cdp(
        {"requestTime": 1.0, "dnsStart": -1, "dnsEnd": -1, "sendStart": 3.0, "sendEnd": 4.0},
        total_ms=None,
    )
    assert timing.dns_ms is None
    assert timing.send_ms == 1.0
    assert timing.wait_ms is None
    assert timing.total_ms is None


def test_har_export_shape_covers_request_response_and_websocket() -> None:
    log = _log()
    session = FakeBodySession({"req-1": ('{"ok":true}', False)})

    async def scenario() -> None:
        await _record_one(log, session, post_data="page=1&size=10")

    asyncio.run(scenario())
    log.on_websocket_created(
        _event("Network.webSocketCreated", {"requestId": "ws-1", "url": "wss://example.com/live"})
    )
    log.on_websocket_frame(
        _event(
            "Network.webSocketFrameSent",
            {"requestId": "ws-1", "timestamp": 1.0, "response": {"opcode": 1, "payloadData": "hi"}},
        ),
        direction="sent",
    )

    document = build_har(log.ordered())
    entry = document["log"]["entries"][0]
    assert document["log"]["version"] == "1.2"
    assert entry["request"]["method"] == "POST"
    assert {"name": "page", "value": "1"} in entry["request"]["queryString"]
    assert entry["request"]["postData"]["text"] == "page=1&size=10"
    assert entry["response"]["content"]["text"] == '{"ok":true}'
    assert entry["timings"]["dns"] == 5.0
    assert entry["_exchangeId"] == "ex-000001"
    websocket = document["log"]["_websockets"][0]
    assert websocket["frames"][0]["data"] == "hi"


def test_har_without_bodies_keeps_metadata_only() -> None:
    log = _log()
    session = FakeBodySession({"req-1": ('{"ok":true}', False)})

    async def scenario() -> None:
        await _record_one(log, session)

    asyncio.run(scenario())
    document = build_har(log.ordered(), include_bodies=False)
    content = document["log"]["entries"][0]["response"]["content"]
    assert "text" not in content
    assert content["size"] == len('{"ok":true}')


def test_traffic_tool_returns_full_data_but_bounded_model_data(tmp_path: Path) -> None:
    log = _log()
    session = FakeBodySession({"req-1": ('{"secret":"leak"}', False)})
    outcome: traffic_tools.TrafficToolOutcome | None = None

    async def scenario() -> None:
        nonlocal outcome
        await _record_one(log, session)
        outcome = await traffic_tools.execute_traffic_tool(
            "inspect_network_traffic",
            {"url_contains": "/api/"},
            _inspector(log, tmp_path),
            task_inputs={},
        )

    asyncio.run(scenario())

    assert outcome is not None
    assert outcome.success is True
    assert outcome.counts_as_action is False
    assert outcome.data["exchanges"][0]["request_headers"]["Accept"] == "application/json"
    model_text = json.dumps(outcome.model_data, ensure_ascii=False)
    assert "leak" not in model_text
    assert "a=1" not in model_text
    assert outcome.model_data["exchanges"][0]["request_header_names"] == ["Accept"]


def test_read_body_tool_keeps_text_out_of_model_view(tmp_path: Path) -> None:
    log = _log()
    session = FakeBodySession({"req-1": ('{"secret":"leak"}', False)})
    outcome: traffic_tools.TrafficToolOutcome | None = None

    async def scenario() -> None:
        nonlocal outcome
        await _record_one(log, session)
        outcome = await traffic_tools.execute_traffic_tool(
            "read_network_body",
            {"exchange_id": "ex-000001"},
            _inspector(log, tmp_path),
            task_inputs={},
        )

    asyncio.run(scenario())

    assert outcome is not None
    assert outcome.data["text"] == '{"secret":"leak"}'
    assert outcome.data["json"] == {"secret": "leak"}
    assert "leak" not in json.dumps(outcome.model_data, ensure_ascii=False)


def test_har_tool_writes_owner_only_file(tmp_path: Path) -> None:
    log = _log()
    session = FakeBodySession({"req-1": ("{}", False)})
    outcome: traffic_tools.TrafficToolOutcome | None = None

    async def scenario() -> None:
        nonlocal outcome
        await _record_one(log, session)
        outcome = await traffic_tools.execute_traffic_tool(
            "export_network_har",
            {"collection_name": "订单流程"},
            _inspector(log, tmp_path),
            task_inputs={},
        )

    asyncio.run(scenario())

    assert outcome is not None
    path = Path(outcome.data["har_path"])
    assert path.exists()
    assert path.suffix == ".har"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert json.loads(path.read_text(encoding="utf-8"))["log"]["version"] == "1.2"
    assert outcome.evidence is not None


def test_traffic_tools_reject_unknown_arguments(tmp_path: Path) -> None:
    log = _log()

    async def scenario() -> None:
        await traffic_tools.execute_traffic_tool(
            "inspect_network_traffic",
            {"limit": 5, "sort_by": "duration"},
            _inspector(log, tmp_path),
            task_inputs={},
        )

    with pytest.raises(ValueError, match="未知参数"):
        asyncio.run(scenario())


def test_missing_exchange_reports_rollout_instead_of_crashing(tmp_path: Path) -> None:
    log = _log()

    async def scenario() -> None:
        await traffic_tools.execute_traffic_tool(
            "read_network_body",
            {"exchange_id": "ex-999999"},
            _inspector(log, tmp_path),
            task_inputs={},
        )

    with pytest.raises(ValueError, match="已被滚动淘汰"):
        asyncio.run(scenario())

from __future__ import annotations

import asyncio
import inspect
import json
import stat
from pathlib import Path
from typing import Any

from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.config import NetworkCaptureConfig
from witty_browser_auto.network.capture import CdpNetworkCapture
from witty_browser_auto.network.recorder import CdpNetworkRecorder


class FakeConnection:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def subscribe(self, method: str, handler: Any, *, session_id: str | None = None) -> Any:
        self.handlers[method] = handler

        def unsubscribe() -> None:
            self.handlers.pop(method, None)

        return unsubscribe


class FakeSession:
    def __init__(self, body: str) -> None:
        self.connection = FakeConnection()
        self.session_id = "session-1"
        self.body = body
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = params or {}
        self.calls.append((method, arguments))
        if method != "Network.getResponseBody":
            raise AssertionError(f"未预期的 CDP 调用：{method}")
        return {"body": self.body, "base64Encoded": False}


async def _dispatch(handler: Any, event: CdpEvent) -> None:
    result = handler(event)
    if inspect.isawaitable(result):
        await result


def _response_event(
    url: str,
    *,
    request_id: str = "request-1",
    mime_type: str = "application/json",
    resource_type: str = "Fetch",
    status: int = 200,
) -> CdpEvent:
    return CdpEvent(
        "Network.responseReceived",
        {
            "requestId": request_id,
            "type": resource_type,
            "response": {
                "url": url,
                "status": status,
                "mimeType": mime_type,
            },
        },
        "session-1",
    )


def _request_event(
    url: str,
    *,
    request_id: str = "request-1",
    post_data: str | None = None,
    timestamp: float | None = None,
) -> CdpEvent:
    request: dict[str, Any] = {"url": url, "method": "POST", "headers": {}}
    if post_data is not None:
        request["postData"] = post_data
    params: dict[str, Any] = {"requestId": request_id, "type": "Fetch", "request": request}
    if timestamp is not None:
        params["timestamp"] = timestamp
    return CdpEvent("Network.requestWillBeSent", params, "session-1")


def _finished_event(
    size: int,
    *,
    request_id: str = "request-1",
    timestamp: float | None = None,
) -> CdpEvent:
    params: dict[str, Any] = {"requestId": request_id, "encodedDataLength": size}
    if timestamp is not None:
        params["timestamp"] = timestamp
    return CdpEvent(
        "Network.loadingFinished",
        params,
        "session-1",
    )


def test_network_capture_exports_private_json_and_csv_without_model_values(tmp_path: Path) -> None:
    async def scenario() -> None:
        body = json.dumps(
            {
                "orders": [
                    {"id": "ORDER-001", "customer": "SENSITIVE-CUSTOMER-A"},
                    {"id": "ORDER-002", "customer": "SENSITIVE-CUSTOMER-B"},
                ],
                "token": "SENSITIVE-RESPONSE-TOKEN",
            }
        )
        session = FakeSession(body)
        capture = CdpNetworkCapture(
            NetworkCaptureConfig(enabled=True, max_body_bytes=1024 * 1024, max_responses=10),
            tmp_path / "artifacts",
            allowed_origins=("https://example.com",),
        )
        recorder = CdpNetworkRecorder(session, capture=capture)  # type: ignore[arg-type]
        await recorder.start()

        session.connection.handlers["Network.requestWillBeSent"](
            _request_event("https://example.com/api/orders?token=secret&page=1")
        )
        session.connection.handlers["Network.responseReceived"](
            _response_event("https://example.com/api/orders?token=secret&page=1")
        )
        await _dispatch(
            session.connection.handlers["Network.loadingFinished"],
            _finished_event(len(body.encode("utf-8"))),
        )

        inspection = await capture.inspect(max_candidates=5)
        serialized = json.dumps(inspection, ensure_ascii=False)
        assert "SENSITIVE-CUSTOMER" not in serialized
        assert "SENSITIVE-RESPONSE-TOKEN" not in serialized
        assert "token=secret" not in serialized
        assert inspection["transport"] == "current_browser_cdp"
        assert inspection["session_reused"] is True
        assert inspection["active_request_count"] == 0
        assert inspection["candidates"][0]["endpoint"] == "https://example.com/api/orders"
        assert inspection["candidates"][0]["method"] == "POST"
        assert inspection["candidates"][0]["json_shape"]["keys"] == ["orders", "token"]

        result = await capture.export(
            inspection["candidates"][0]["candidate_id"],
            "订单接口",
        )

        assert result.record_count == 2
        assert result.complete is False
        assert result.captured_response_count == 1
        assert result.visited_pages == ()
        assert result.completion_evidence == ()
        assert any("单个" in reason for reason in result.failure_reasons)
        assert result.json_path is not None and result.json_path.is_file()
        assert result.csv_path is not None and result.csv_path.is_file()
        assert stat.S_IMODE(result.json_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.csv_path.stat().st_mode) == 0o600
        assert "SENSITIVE-CUSTOMER-A" in result.json_path.read_text(encoding="utf-8")
        assert "SENSITIVE-CUSTOMER-A" not in str(result.model_summary())
        assert result.model_summary()["complete"] is False
        await recorder.close()

    asyncio.run(scenario())


def test_network_capture_rejects_cross_origin_and_oversized_bodies(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = FakeSession('{"ok": true}')
        capture = CdpNetworkCapture(
            NetworkCaptureConfig(enabled=True, max_body_bytes=1024, max_responses=10),
            tmp_path / "artifacts",
            allowed_origins=("https://example.com",),
        )
        recorder = CdpNetworkRecorder(session, capture=capture)  # type: ignore[arg-type]
        await recorder.start()

        session.connection.handlers["Network.responseReceived"](
            _response_event("https://outside.example/api/data", request_id="cross-origin")
        )
        await _dispatch(
            session.connection.handlers["Network.loadingFinished"],
            _finished_event(10, request_id="cross-origin"),
        )
        session.connection.handlers["Network.responseReceived"](
            _response_event("https://example.com/api/large", request_id="too-large")
        )
        await _dispatch(
            session.connection.handlers["Network.loadingFinished"],
            _finished_event(4096, request_id="too-large"),
        )

        assert await capture.inspect() == {
            "candidates": [],
            "captured_count": 0,
            "transport": "current_browser_cdp",
            "session_reused": True,
            "active_request_count": 0,
        }
        assert session.calls == []
        await recorder.close()

    asyncio.run(scenario())


def test_network_capture_aggregates_paginated_candidates_with_count_closure(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        session = FakeSession("")
        capture = CdpNetworkCapture(
            NetworkCaptureConfig(enabled=True, max_body_bytes=1024 * 1024, max_responses=10),
            tmp_path / "artifacts",
            allowed_origins=("https://example.com",),
        )
        recorder = CdpNetworkRecorder(session, capture=capture)  # type: ignore[arg-type]
        await recorder.start()

        bodies = (
            {"data": {"records": [{"id": 1}, {"id": 2}], "total": 3, "pages": 2, "page": 1}},
            {"data": {"records": [{"id": 2}, {"id": 3}], "total": 3, "pages": 2, "page": 2}},
        )
        for page, payload in enumerate(bodies, start=1):
            request_id = f"request-{page}"
            session.body = json.dumps(payload)
            url = f"https://example.com/api/orders?page={page}"
            session.connection.handlers["Network.requestWillBeSent"](
                _request_event(url, request_id=request_id)
            )
            session.connection.handlers["Network.responseReceived"](
                _response_event(url, request_id=request_id)
            )
            await _dispatch(
                session.connection.handlers["Network.loadingFinished"],
                _finished_event(len(session.body.encode("utf-8")), request_id=request_id),
            )

        inspection = await capture.inspect(max_candidates=10)
        candidate_ids = [candidate["candidate_id"] for candidate in inspection["candidates"]]
        result = await capture.export_many(candidate_ids, "全部订单详情")

        assert result.complete is True
        assert result.has_strong_completion_evidence is True
        assert result.record_count == 3
        assert result.captured_response_count == 2
        assert result.declared_total == 3
        assert result.declared_pages == 2
        assert result.visited_pages == (1, 2)
        assert result.csv_path is not None
        assert len(result.csv_path.read_text(encoding="utf-8-sig").splitlines()) == 4
        assert stat.S_IMODE(result.json_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.csv_path.stat().st_mode) == 0o600
        await recorder.close()

    asyncio.run(scenario())


def _build_capture(tmp_path: Path, session: FakeSession) -> tuple[CdpNetworkCapture, Any]:
    capture = CdpNetworkCapture(
        NetworkCaptureConfig(enabled=True, max_body_bytes=1024 * 1024, max_responses=10),
        tmp_path / "artifacts",
        allowed_origins=("https://example.com",),
    )
    recorder = CdpNetworkRecorder(session, capture=capture)  # type: ignore[arg-type]
    return capture, recorder


def test_network_capture_records_request_shape_and_duration(tmp_path: Path) -> None:
    async def scenario() -> None:
        body = json.dumps({"items": [{"id": 1}]})
        session = FakeSession(body)
        capture, recorder = _build_capture(tmp_path, session)
        await recorder.start()
        handlers = session.connection.handlers

        handlers["Network.requestWillBeSent"](
            _request_event(
                "https://example.com/api/orders",
                post_data=json.dumps({"phone": "13800000000", "page": 1}),
                timestamp=100.0,
            )
        )
        handlers["Network.responseReceived"](_response_event("https://example.com/api/orders"))
        await _dispatch(
            handlers["Network.loadingFinished"],
            _finished_event(len(body.encode("utf-8")), timestamp=100.35),
        )

        handlers["Network.requestWillBeSent"](
            _request_event(
                "https://example.com/api/search",
                request_id="request-2",
                post_data="keyword=abc&page=2",
            )
        )
        handlers["Network.responseReceived"](
            _response_event("https://example.com/api/search", request_id="request-2")
        )
        await _dispatch(
            handlers["Network.loadingFinished"],
            _finished_event(len(body.encode("utf-8")), request_id="request-2"),
        )

        inspection = await capture.inspect(max_candidates=5)
        serialized = json.dumps(inspection, ensure_ascii=False)
        assert "13800000000" not in serialized
        assert "abc" not in serialized
        by_endpoint = {item["endpoint"]: item for item in inspection["candidates"]}
        orders = by_endpoint["https://example.com/api/orders"]
        assert orders["duration_ms"] == 350
        assert orders["request_shape"]["type"] == "object"
        assert orders["request_shape"]["keys"] == ["phone", "page"]
        search = by_endpoint["https://example.com/api/search"]
        assert search["request_shape"]["type"] == "form"
        assert search["request_shape"]["keys"] == ["keyword", "page"]
        assert search["duration_ms"] is None
        await recorder.close()

    asyncio.run(scenario())


def test_wait_for_response_resolves_when_matching_json_arrives(tmp_path: Path) -> None:
    async def scenario() -> None:
        body = json.dumps({"rows": [{"id": 1}], "total": 1})
        session = FakeSession(body)
        capture, recorder = _build_capture(tmp_path, session)
        await recorder.start()
        handlers = session.connection.handlers

        wait_task = asyncio.create_task(capture.wait_for_response("/api/orders", timeout_seconds=5))
        await asyncio.sleep(0)
        handlers["Network.requestWillBeSent"](
            _request_event("https://example.com/api/orders?page=1", timestamp=10.0)
        )
        handlers["Network.responseReceived"](
            _response_event("https://example.com/api/orders?page=1")
        )
        await _dispatch(
            handlers["Network.loadingFinished"],
            _finished_event(len(body.encode("utf-8")), timestamp=10.12),
        )
        result = await wait_task
        assert result["matched"] is True
        assert result["captured"] is True
        assert result["endpoint"] == "https://example.com/api/orders"
        assert result["candidate_id"]
        assert result["duration_ms"] == 120

        immediate = await capture.wait_for_response("/api/orders", timeout_seconds=5)
        assert immediate["captured"] is True
        assert immediate["candidate_id"] == result["candidate_id"]
        await recorder.close()

    asyncio.run(scenario())


def test_wait_for_response_reports_uncaptured_and_failed_requests(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = FakeSession("")
        capture, recorder = _build_capture(tmp_path, session)
        await recorder.start()
        handlers = session.connection.handlers

        html_task = asyncio.create_task(capture.wait_for_response("/page.html", timeout_seconds=5))
        await asyncio.sleep(0)
        handlers["Network.requestWillBeSent"](
            _request_event("https://example.com/page.html", request_id="doc-1", timestamp=1.0)
        )
        handlers["Network.responseReceived"](
            _response_event(
                "https://example.com/page.html",
                request_id="doc-1",
                mime_type="text/html",
                resource_type="Document",
            )
        )
        await _dispatch(
            handlers["Network.loadingFinished"],
            _finished_event(120, request_id="doc-1", timestamp=1.2),
        )
        html_result = await html_task
        assert html_result["matched"] is True
        assert html_result["captured"] is False
        assert html_result["status"] == 200
        assert html_result["mime_type"] == "text/html"
        assert session.calls == []

        fail_task = asyncio.create_task(capture.wait_for_response("/api/broken", timeout_seconds=5))
        await asyncio.sleep(0)
        handlers["Network.requestWillBeSent"](
            _request_event("https://example.com/api/broken", request_id="broken-1")
        )
        handlers["Network.loadingFailed"](
            CdpEvent(
                "Network.loadingFailed",
                {"requestId": "broken-1", "type": "Fetch", "errorText": "net::ERR_FAILED"},
                "session-1",
            )
        )
        fail_result = await fail_task
        assert fail_result["matched"] is True
        assert fail_result["captured"] is False
        assert fail_result["failed"] is True
        await recorder.close()

    asyncio.run(scenario())


def test_wait_for_response_times_out_without_matching_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = FakeSession("")
        capture, recorder = _build_capture(tmp_path, session)
        await recorder.start()

        result = await capture.wait_for_response("/api/none", timeout_seconds=1)
        assert result == {
            "matched": False,
            "url_substring": "/api/none",
            "waited_seconds": 1.0,
        }
        assert capture._waiters == []
        await recorder.close()

    asyncio.run(scenario())

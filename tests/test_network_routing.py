from __future__ import annotations

import asyncio
import base64
import inspect
from typing import Any

import pytest

from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.config import NetworkCaptureConfig
from witty_browser_auto.network.capture import CdpNetworkCapture
from witty_browser_auto.network.recorder import CdpNetworkRecorder
from witty_browser_auto.network.routing import CdpNetworkRouter, ReplayInterception


class FakeConnection:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def subscribe(self, method: str, handler: Any, *, session_id: str | None = None) -> Any:
        self.handlers[method] = handler

        def unsubscribe() -> None:
            self.handlers.pop(method, None)

        return unsubscribe


class FakeSession:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.session_id = "session-1"
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        values = params or {}
        self.calls.append((method, values))
        if method == "Fetch.getResponseBody":
            return {"body": base64.b64encode(b'{"original":true}').decode(), "base64Encoded": True}
        return {}


async def dispatch(handler: Any, event: CdpEvent) -> None:
    result = handler(event)
    if inspect.isawaitable(result):
        await result


def _paused_event(
    *,
    url: str,
    request_id: str = "request-1",
    method: str = "GET",
    response: bool = False,
) -> CdpEvent:
    params: dict[str, Any] = {
        "requestId": request_id,
        "request": {"url": url, "method": method, "headers": {"Accept": "application/json"}},
    }
    if response:
        params.update(
            {
                "responseStatusCode": 200,
                "responseHeaders": [{"name": "Content-Type", "value": "application/json"}],
            }
        )
    return CdpEvent("Fetch.requestPaused", params, "session-1")


def test_network_routes_block_and_modify_request(tmp_path) -> None:
    async def scenario() -> None:
        session = FakeSession()
        capture = CdpNetworkCapture(
            NetworkCaptureConfig(enabled=True),
            tmp_path,
            allowed_origins=("https://example.com",),
        )
        recorder = CdpNetworkRecorder(session, capture=capture)  # type: ignore[arg-type]
        await recorder.start()
        await capture.manage_route(
            "add",
            {"url_pattern": "https://example.com/api/blocked*", "action": "block"},
        )
        await dispatch(
            session.connection.handlers["Fetch.requestPaused"],
            _paused_event(url="https://example.com/api/blocked?id=1"),
        )
        assert session.calls[-1][0] == "Fetch.failRequest"

        await capture.manage_route(
            "add",
            {
                "url_pattern": "https://example.com/api/update*",
                "action": "modify_request",
                "request_method": "POST",
                "request_body": "payload",
                "request_headers": {"X-Test": "enabled"},
            },
        )
        await dispatch(
            session.connection.handlers["Fetch.requestPaused"],
            _paused_event(url="https://example.com/api/update", method="PUT"),
        )
        method, params = session.calls[-1]
        assert method == "Fetch.continueRequest"
        assert params["method"] == "POST"
        assert params["postData"] == base64.b64encode(b"payload").decode()
        assert {item["name"] for item in params["headers"]} == {"Accept", "X-Test"}
        await recorder.close()

    asyncio.run(scenario())


def test_network_routes_mock_and_modify_response(tmp_path) -> None:
    async def scenario() -> None:
        session = FakeSession()
        capture = CdpNetworkCapture(
            NetworkCaptureConfig(enabled=True),
            tmp_path,
            allowed_origins=("https://example.com",),
        )
        recorder = CdpNetworkRecorder(session, capture=capture)  # type: ignore[arg-type]
        await recorder.start()
        await capture.manage_route(
            "add",
            {
                "url_pattern": "https://example.com/api/mock",
                "action": "mock_response",
                "response_status": 201,
                "response_body": '{"mocked":true}',
            },
        )
        await dispatch(
            session.connection.handlers["Fetch.requestPaused"],
            _paused_event(url="https://example.com/api/mock"),
        )
        method, params = session.calls[-1]
        assert method == "Fetch.fulfillRequest"
        assert params["responseCode"] == 201
        assert base64.b64decode(params["body"]).decode() == '{"mocked":true}'

        await capture.manage_route(
            "add",
            {
                "url_pattern": "https://example.com/api/replace",
                "action": "modify_response",
                "response_status": 201,
            },
        )
        await dispatch(
            session.connection.handlers["Fetch.requestPaused"],
            _paused_event(url="https://example.com/api/replace", response=True),
        )
        assert session.calls[-2][0] == "Fetch.getResponseBody"
        assert session.calls[-1][0] == "Fetch.fulfillRequest"
        assert base64.b64decode(session.calls[-1][1]["body"]).decode() == '{"original":true}'
        assert session.calls[-1][1]["responseCode"] == 201
        await recorder.close()

    asyncio.run(scenario())


def test_modify_response_preserves_status_when_only_headers_change(tmp_path) -> None:
    async def scenario() -> None:
        session = FakeSession()
        capture = CdpNetworkCapture(
            NetworkCaptureConfig(enabled=True),
            tmp_path,
            allowed_origins=("https://example.com",),
        )
        recorder = CdpNetworkRecorder(session, capture=capture)  # type: ignore[arg-type]
        await recorder.start()
        await capture.manage_route(
            "add",
            {
                "url_pattern": "https://example.com/api/status",
                "action": "modify_response",
                "response_headers": {"X-Debug": "enabled"},
            },
        )
        event = _paused_event(url="https://example.com/api/status", response=True)
        event.params["responseStatusCode"] = 404
        await dispatch(session.connection.handlers["Fetch.requestPaused"], event)

        assert session.calls[-1][0] == "Fetch.fulfillRequest"
        assert session.calls[-1][1]["responseCode"] == 404
        assert {item["name"] for item in session.calls[-1][1]["responseHeaders"]} == {
            "Content-Type",
            "X-Debug",
        }
        await recorder.close()

    asyncio.run(scenario())


def test_modify_response_preserves_binary_body_and_overrides_header_case(tmp_path) -> None:
    async def scenario() -> None:
        session = FakeSession()
        original_bytes = b"\x00\xff\x10binary"

        async def call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            values = params or {}
            session.calls.append((method, values))
            if method == "Fetch.getResponseBody":
                return {"body": base64.b64encode(original_bytes).decode(), "base64Encoded": True}
            return {}

        session.call = call  # type: ignore[method-assign]
        capture = CdpNetworkCapture(
            NetworkCaptureConfig(enabled=True),
            tmp_path,
            allowed_origins=("https://example.com",),
        )
        recorder = CdpNetworkRecorder(session, capture=capture)  # type: ignore[arg-type]
        await recorder.start()
        await capture.manage_route(
            "add",
            {
                "url_pattern": "https://example.com/api/binary",
                "action": "modify_response",
                "response_headers": {"content-type": "application/octet-stream"},
            },
        )
        await dispatch(
            session.connection.handlers["Fetch.requestPaused"],
            _paused_event(url="https://example.com/api/binary", response=True),
        )

        fulfilled = session.calls[-1][1]
        assert base64.b64decode(fulfilled["body"]) == original_bytes
        assert fulfilled["responseHeaders"] == [
            {"name": "content-type", "value": "application/octet-stream"}
        ]
        await recorder.close()

    asyncio.run(scenario())


def test_network_route_allows_sensitive_headers_but_rejects_browser_managed_headers(
    tmp_path,
) -> None:
    async def scenario() -> None:
        session = FakeSession()
        capture = CdpNetworkCapture(
            NetworkCaptureConfig(enabled=True),
            tmp_path,
            allowed_origins=("https://example.com",),
        )
        recorder = CdpNetworkRecorder(session, capture=capture)  # type: ignore[arg-type]
        await recorder.start()
        with pytest.raises(ValueError, match="允许的 HTTP/HTTPS origin"):
            await capture.manage_route(
                "add",
                {"url_pattern": "https://outside.example/*", "action": "block"},
            )
        await capture.manage_route(
            "add",
            {
                "url_pattern": "https://example.com/api/private*",
                "action": "modify_request",
                "request_headers": {
                    "Authorization": "Bearer private",
                    "Cookie": "session=private",
                    "Host": "api.internal.example",
                },
            },
        )
        await dispatch(
            session.connection.handlers["Fetch.requestPaused"],
            _paused_event(url="https://example.com/api/private"),
        )
        sent_headers = {item["name"]: item["value"] for item in session.calls[-1][1]["headers"]}
        assert sent_headers["Authorization"] == "Bearer private"
        assert sent_headers["Cookie"] == "session=private"
        assert "Host" not in sent_headers
        assert session.calls[-1][1]["url"] == "https://api.internal.example/api/private"

        with pytest.raises(ValueError, match="浏览器管理的 Header"):
            await capture.manage_route(
                "add",
                {
                    "url_pattern": "https://example.com/*",
                    "action": "modify_request",
                    "request_headers": {"Content-Length": "999"},
                },
            )
        with pytest.raises(ValueError, match="Header 名称格式无效"):
            await capture.manage_route(
                "add",
                {
                    "url_pattern": "https://example.com/*",
                    "action": "modify_request",
                    "request_headers": {"Bad Header": "value"},
                },
            )
        await recorder.close()

    asyncio.run(scenario())


def _replay_router(session: FakeSession) -> CdpNetworkRouter:
    return CdpNetworkRouter(session, ("https://example.com",))  # type: ignore[arg-type]


def test_replay_interception_strips_headers_chrome_would_reject() -> None:
    """真实 Chrome 对 Fetch.continueRequest 里的 Host 直接报 Unsafe header。"""

    async def scenario() -> None:
        session = FakeSession()
        router = _replay_router(session)
        await router.arm_replay(
            ReplayInterception(
                url="https://example.com/api/orders",
                method="POST",
                header_overrides=(("Cookie", "session=replayed"),),
            )
        )
        event = CdpEvent(
            "Fetch.requestPaused",
            {
                "requestId": "request-9",
                "request": {
                    "url": "https://example.com/api/orders",
                    "method": "POST",
                    "headers": {
                        "Host": "example.com",
                        "Connection": "keep-alive",
                        "Cookie": "session=original",
                        "Accept": "*/*",
                    },
                },
            },
            "session-1",
        )
        await dispatch(session.connection.handlers["Fetch.requestPaused"], event)

        method, params = session.calls[-1]
        assert method == "Fetch.continueRequest"
        sent = {item["name"].casefold(): item["value"] for item in params["headers"]}
        assert sent["cookie"] == "session=replayed"
        assert "host" not in sent
        assert "connection" not in sent
        await router.close()

    asyncio.run(scenario())


def test_replay_interception_sends_status_and_headers_together_on_response_stage() -> None:
    """continueResponse 只给响应头会被 Chrome 拒绝，状态码必须一起下发。"""

    async def scenario() -> None:
        session = FakeSession()
        router = _replay_router(session)
        await router.arm_replay(
            ReplayInterception(
                url="https://example.com/api/orders",
                method="GET",
                page_origin="https://example.com",
            )
        )
        await dispatch(
            session.connection.handlers["Fetch.requestPaused"],
            _paused_event(url="https://example.com/api/orders", response=True),
        )

        method, params = session.calls[-1]
        assert method == "Fetch.continueResponse"
        assert params["responseCode"] == 200
        exposed = {item["name"].casefold(): item["value"] for item in params["responseHeaders"]}
        assert exposed["access-control-expose-headers"] == "*"
        await router.close()

    asyncio.run(scenario())


def test_disarming_replay_stops_rewriting_later_requests() -> None:
    async def scenario() -> None:
        session = FakeSession()
        router = _replay_router(session)
        await router.arm_replay(
            ReplayInterception(
                url="https://example.com/api/orders",
                method="GET",
                header_overrides=(("X-Trace", "replay"),),
            )
        )
        await router.disarm_replay()
        await dispatch(
            session.connection.handlers["Fetch.requestPaused"],
            _paused_event(url="https://example.com/api/orders"),
        )

        method, params = session.calls[-1]
        assert method == "Fetch.continueRequest"
        assert "headers" not in params, "撤销后不应再改写请求"
        await router.close()

    asyncio.run(scenario())

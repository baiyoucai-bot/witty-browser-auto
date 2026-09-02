from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

import witty_browser_auto.browser.session as browser_session_module
from witty_browser_auto.browser.session import CdpBrowser, CdpTargetSession
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.cdp.transport import CdpConnection
from witty_browser_auto.config import BrowserConfig
from witty_browser_auto.domain.errors import (
    CdpCommandError,
    CdpConnectionError,
    CdpDisconnectedError,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        item = await self.queue.get()
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        await self.queue.put(StopAsyncIteration)

    async def receive_payload(self, payload: dict[str, Any]) -> None:
        await self.queue.put(json.dumps(payload))


def test_takeover_start_never_invokes_chromium_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing_live_endpoint() -> None:
        return None

    class ForbiddenLauncher:
        def __init__(self, config: BrowserConfig) -> None:
            raise AssertionError("接管模式不得创建 ChromiumLauncher")

    async def browser_not_running() -> bool:
        return False

    monkeypatch.setattr(
        browser_session_module,
        "discover_live_browser_endpoint",
        missing_live_endpoint,
    )
    monkeypatch.setattr(
        browser_session_module,
        "open_live_browser_authorization_page",
        browser_not_running,
    )
    monkeypatch.setattr(browser_session_module, "ChromiumLauncher", ForbiddenLauncher)

    async def scenario() -> None:
        browser = CdpBrowser(BrowserConfig(session_mode="takeover"))  # type: ignore[arg-type]
        with pytest.raises(CdpConnectionError, match="未检测到可接管的运行中 Chrome"):
            await browser.start()
        assert browser.managed_process is None

    asyncio.run(scenario())


def test_detached_event_aborts_session_pending_commands_immediately() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        connection = CdpConnection("ws://127.0.0.1/devtools", websocket=websocket)
        await connection.connect()

        browser = CdpBrowser(BrowserConfig(cdp_endpoint="http://127.0.0.1:9222"))
        browser.connection = connection
        session = CdpTargetSession(connection, "target-1", "session-1")
        browser._register_session(session)

        command = asyncio.create_task(session.call("Runtime.evaluate"))
        await asyncio.sleep(0)

        browser._on_detached(
            CdpEvent(
                "Target.detachedFromTarget",
                {"sessionId": "session-1", "targetId": "target-1"},
            )
        )

        with pytest.raises(CdpDisconnectedError) as error:
            await command
        assert "CDP 会话已断开：session-1" in str(error.value)
        assert "session-1" not in browser._sessions_by_id
        assert "target-1" not in browser._sessions_by_target
        await connection.close()

    asyncio.run(scenario())


def test_headless_target_creation_retries_with_new_window() -> None:
    class StubConnection:
        closed = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            payload = params or {}
            self.calls.append((method, payload))
            if len(self.calls) == 1:
                raise CdpCommandError(
                    "Failed to open new tab - no browser is open",
                    method=method,
                    error_code=-32000,
                )
            return {"targetId": "target-1"}

    async def scenario() -> None:
        browser = CdpBrowser(BrowserConfig(cdp_endpoint="http://127.0.0.1:9222"))
        connection = StubConnection()
        browser.connection = connection  # type: ignore[assignment]

        result = await browser._create_target("context-1", "about:blank")

        assert result == {"targetId": "target-1"}
        assert connection.calls == [
            (
                "Target.createTarget",
                {"url": "about:blank", "browserContextId": "context-1"},
            ),
            (
                "Target.createTarget",
                {
                    "url": "about:blank",
                    "browserContextId": "context-1",
                    "newWindow": True,
                },
            ),
        ]

    asyncio.run(scenario())


def test_default_context_target_does_not_send_browser_context_id() -> None:
    class StubConnection:
        closed = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            payload = params or {}
            self.calls.append((method, payload))
            return {"targetId": "target-default"}

    async def scenario() -> None:
        browser = CdpBrowser(BrowserConfig(cdp_endpoint="http://127.0.0.1:9222"))
        connection = StubConnection()
        browser.connection = connection  # type: ignore[assignment]

        result = await browser._create_target(None, "about:blank")

        assert result == {"targetId": "target-default"}
        assert connection.calls == [("Target.createTarget", {"url": "about:blank"})]

    asyncio.run(scenario())


def test_explicit_window_target_sets_new_window_flag() -> None:
    class StubConnection:
        closed = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            payload = params or {}
            self.calls.append((method, payload))
            return {"targetId": "task-window"}

    async def scenario() -> None:
        browser = CdpBrowser(BrowserConfig(cdp_endpoint="http://127.0.0.1:9222"))
        connection = StubConnection()
        browser.connection = connection  # type: ignore[assignment]

        result = await browser._create_target(
            None,
            "https://example.com/orders",
            new_window=True,
        )

        assert result == {"targetId": "task-window"}
        assert connection.calls == [
            (
                "Target.createTarget",
                {"url": "https://example.com/orders", "newWindow": True},
            )
        ]

    asyncio.run(scenario())


def test_wait_for_target_session_initializes_delayed_auto_attached_page() -> None:
    class StubSession:
        def __init__(self) -> None:
            self.initialized = 0

        async def initialize(self) -> None:
            self.initialized += 1

    async def scenario() -> None:
        browser = CdpBrowser(BrowserConfig(cdp_endpoint="http://127.0.0.1:9222"))
        session = StubSession()

        async def register_later() -> None:
            await asyncio.sleep(0)
            browser._sessions_by_target["new-page"] = session  # type: ignore[assignment]

        register_task = asyncio.create_task(register_later())
        resolved = await browser.wait_for_target_session("new-page", timeout_seconds=0.2)
        await register_task

        assert resolved is session
        assert session.initialized == 1

    asyncio.run(scenario())


def test_destroyed_target_aborts_session_pending_commands_immediately() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        connection = CdpConnection("ws://127.0.0.1/devtools", websocket=websocket)
        await connection.connect()

        browser = CdpBrowser(BrowserConfig(cdp_endpoint="http://127.0.0.1:9222"))
        browser.connection = connection
        session = CdpTargetSession(connection, "target-1", "session-1")
        browser._register_session(session)

        command = asyncio.create_task(session.call("Runtime.evaluate"))
        await asyncio.sleep(0)
        browser._on_destroyed(CdpEvent("Target.targetDestroyed", {"targetId": "target-1"}))

        with pytest.raises(CdpDisconnectedError, match="页面 Target 已销毁"):
            await command
        assert "session-1" not in browser._sessions_by_id
        assert "target-1" not in browser._sessions_by_target
        await connection.close()

    asyncio.run(scenario())


def test_claim_existing_page_prefers_persisted_target() -> None:
    class StubConnection:
        closed = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            payload = params or {}
            self.calls.append((method, payload))
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"targetId": "blank", "type": "page", "url": "about:blank"},
                        {
                            "targetId": "other-page",
                            "type": "page",
                            "url": "https://example.com/other",
                        },
                        {
                            "targetId": "order-page",
                            "type": "page",
                            "url": "https://example.com/order",
                        },
                    ]
                }
            if method == "Target.attachToTarget":
                return {"sessionId": "reattached-session"}
            return {}

        def subscribe(self, *args: Any, **kwargs: Any) -> object:
            return lambda: None

    async def scenario() -> None:
        browser = CdpBrowser(BrowserConfig(cdp_endpoint="http://127.0.0.1:9222"))
        connection = StubConnection()
        browser.connection = connection  # type: ignore[assignment]
        browser._reattach_target_id = "order-page"

        session = await browser.claim_existing_page()

        assert session is not None
        assert session.target_id == "order-page"
        assert (
            "Target.attachToTarget",
            {"targetId": "order-page", "flatten": True},
        ) in connection.calls

    asyncio.run(scenario())


def test_claim_existing_page_prefers_focused_page() -> None:
    class StubConnection:
        closed = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any], str | None]] = []

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            *,
            session_id: str | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            payload = params or {}
            self.calls.append((method, payload, session_id))
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {
                            "targetId": "background-page",
                            "type": "page",
                            "url": "https://example.com/background",
                        },
                        {
                            "targetId": "focused-page",
                            "type": "page",
                            "url": "https://example.com/current",
                        },
                    ]
                }
            if method == "Target.attachToTarget":
                return {"sessionId": f"session-{payload['targetId']}"}
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "value": {
                            "focused": session_id == "session-focused-page",
                            "visibility": "visible",
                        }
                    }
                }
            return {}

        def subscribe(self, *args: Any, **kwargs: Any) -> object:
            return lambda: None

    async def scenario() -> None:
        browser = CdpBrowser(BrowserConfig(cdp_endpoint="http://127.0.0.1:9222"))
        connection = StubConnection()
        browser.connection = connection  # type: ignore[assignment]

        session = await browser.claim_existing_page()

        assert session is not None
        assert session.target_id == "focused-page"

    asyncio.run(scenario())

from __future__ import annotations

import asyncio
from typing import Any

from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.network.recorder import CdpNetworkRecorder
from witty_browser_auto.security.redaction import REDACTED


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


def test_network_recorder_masks_auth_and_query_tokens() -> None:
    async def scenario() -> None:
        session = FakeSession()
        recorder = CdpNetworkRecorder(session)  # type: ignore[arg-type]
        await recorder.start()
        session.connection.handlers["Network.requestWillBeSent"](
            CdpEvent(
                "Network.requestWillBeSent",
                {
                    "requestId": "request-1",
                    "type": "XHR",
                    "request": {
                        "url": "https://example.com/api?token=secret&page=1",
                        "method": "GET",
                        "headers": {"Authorization": "Bearer secret", "Accept": "application/json"},
                    },
                },
                "session-1",
            )
        )

        snapshot = await recorder.snapshot()

        assert "secret" not in snapshot[0]["url"]
        assert snapshot[0]["headers"]["Authorization"] == REDACTED
        assert snapshot[0]["headers"]["Accept"] == "application/json"
        await recorder.close()

    asyncio.run(scenario())


def test_network_recorder_keeps_redacted_url_and_protocol_reason_on_failure() -> None:
    async def scenario() -> None:
        session = FakeSession()
        recorder = CdpNetworkRecorder(session)  # type: ignore[arg-type]
        await recorder.start()
        session.connection.handlers["Network.requestWillBeSent"](
            CdpEvent(
                "Network.requestWillBeSent",
                {
                    "requestId": "request-failed",
                    "type": "Fetch",
                    "request": {
                        "url": "https://example.com/challenge?token=secret",
                        "method": "POST",
                    },
                },
                "session-1",
            )
        )
        session.connection.handlers["Network.loadingFailed"](
            CdpEvent(
                "Network.loadingFailed",
                {
                    "requestId": "request-failed",
                    "type": "Fetch",
                    "errorText": "net::ERR_FAILED",
                    "blockedReason": "inspector",
                    "corsErrorStatus": {"corsError": "DisallowedByMode"},
                },
                "session-1",
            )
        )

        failed = (await recorder.snapshot())[-1]

        assert failed["event"] == "failed"
        assert failed["url"].startswith("https://example.com/challenge?token=")
        assert "secret" not in failed["url"]
        assert failed["failed_reason"] == (
            "net::ERR_FAILED; blocked:inspector; cors:DisallowedByMode"
        )
        await recorder.close()

    asyncio.run(scenario())

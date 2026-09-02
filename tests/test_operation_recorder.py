from __future__ import annotations

import asyncio
import json

from witty_browser_auto.browser.operation_recorder import CdpUserOperationRecorder
from witty_browser_auto.cdp.protocol import CdpEvent


class FakeConnection:
    def __init__(self) -> None:
        self.handler = None
        self.unsubscribed = False

    def subscribe(self, method, handler, *, session_id=None):
        assert method == "Runtime.bindingCalled"
        assert session_id == "session-1"
        self.handler = handler

        def unsubscribe() -> None:
            self.unsubscribed = True

        return unsubscribe


class FakeSession:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.session_id = "session-1"
        self.calls = []

    async def call(self, method, params=None):
        self.calls.append((method, params or {}))
        return {}


def test_operation_recorder_installs_and_sanitizes_events() -> None:
    async def scenario() -> None:
        operations = []
        session = FakeSession()
        recorder = CdpUserOperationRecorder(operations.append)

        await recorder.start(session)
        payload = {
            "kind": "change",
            "source": "user",
            "trusted": True,
            "url": "https://example.com/form?token=secret",
            "tag": "input",
            "input_type": "password",
            "text": "should-not-survive",
            "value": "actual-password",
            "attributes": {"name": "password", "value": "actual-password"},
        }
        await session.connection.handler(
            CdpEvent(
                method="Runtime.bindingCalled",
                params={"name": "__wittyRecordOperation", "payload": json.dumps(payload)},
                session_id="session-1",
            )
        )
        await recorder.set_agent_action(True)
        await recorder.close()

        assert [call[0] for call in session.calls[:3]] == [
            "Runtime.addBinding",
            "Page.addScriptToEvaluateOnNewDocument",
            "Runtime.evaluate",
        ]
        assert operations[0]["source"] == "user"
        assert "secret" not in operations[0]["url"]
        assert "value" not in operations[0]
        assert operations[0]["attributes"] == {"name": "password"}
        assert session.connection.unsubscribed is True

    asyncio.run(scenario())

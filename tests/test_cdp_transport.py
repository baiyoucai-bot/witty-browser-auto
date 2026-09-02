from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from witty_browser_auto.cdp.transport import CdpConnection
from witty_browser_auto.domain.errors import CdpCommandError, CdpDisconnectedError


class FakeWebSocket:
    def __init__(
        self,
        *,
        send_error: Exception | None = None,
        block_send: bool = False,
    ) -> None:
        self.sent: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False
        self.send_error = send_error
        self.block_send = block_send
        self.send_gate = asyncio.Event()

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.send_error is not None:
            raise self.send_error
        if self.block_send:
            await self.send_gate.wait()
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


def test_cdp_call_correlates_response_and_session() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        connection = CdpConnection("ws://127.0.0.1/devtools", websocket=websocket)
        await connection.connect()

        call_task = asyncio.create_task(
            connection.call("Runtime.evaluate", {"expression": "1+1"}, session_id="session-1")
        )
        await asyncio.sleep(0)
        sent = websocket.sent[0]
        await websocket.receive_payload({"id": sent["id"], "result": {"value": 2}})

        assert await call_task == {"value": 2}
        assert sent["sessionId"] == "session-1"
        await connection.close()

    asyncio.run(scenario())


def test_event_waiter_is_registered_before_command() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        connection = CdpConnection("ws://127.0.0.1/devtools", websocket=websocket)
        await connection.connect()

        async with connection.expect_event("Page.loadEventFired", session_id="s1") as event_future:
            await websocket.receive_payload(
                {"method": "Page.loadEventFired", "params": {"timestamp": 1}, "sessionId": "s1"}
            )
            event = await asyncio.wait_for(event_future, timeout=1)

        assert event.params["timestamp"] == 1
        await connection.close()

    asyncio.run(scenario())


def test_protocol_error_becomes_typed_exception() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        connection = CdpConnection("ws://127.0.0.1/devtools", websocket=websocket)
        await connection.connect()

        call_task = asyncio.create_task(connection.call("Unknown.method"))
        await asyncio.sleep(0)
        await websocket.receive_payload(
            {"id": websocket.sent[0]["id"], "error": {"code": -32601, "message": "not found"}}
        )

        with pytest.raises(CdpCommandError) as error:
            await call_task
        assert error.value.error_code == -32601
        assert error.value.method == "Unknown.method"
        await connection.close()

    asyncio.run(scenario())


def test_send_json_failure_clears_pending_command() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket(send_error=RuntimeError("socket gone"))
        connection = CdpConnection("ws://127.0.0.1/devtools", websocket=websocket)
        await connection.connect()

        with pytest.raises(CdpDisconnectedError) as error:
            await connection.call("Page.enable", session_id="session-1")

        assert "发送 CDP 命令时连接中断" in str(error.value)
        assert connection._pending == {}
        await connection.close()

    asyncio.run(scenario())


def test_timeout_clears_pending_command() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        connection = CdpConnection(
            "ws://127.0.0.1/devtools",
            websocket=websocket,
            command_timeout_seconds=0.01,
        )
        await connection.connect()

        with pytest.raises(CdpCommandError) as error:
            await connection.call("Page.enable", session_id="session-1")

        assert error.value.method == "Page.enable"
        assert connection._pending == {}
        await connection.close()

    asyncio.run(scenario())


def test_timeout_includes_websocket_write_and_send_lock() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket(block_send=True)
        connection = CdpConnection(
            "ws://127.0.0.1/devtools",
            websocket=websocket,
            command_timeout_seconds=0.01,
        )
        await connection.connect()

        with pytest.raises(CdpCommandError) as error:
            await connection.call("Page.getLayoutMetrics", session_id="session-1")

        assert error.value.method == "Page.getLayoutMetrics"
        assert connection._pending == {}
        assert not connection._send_lock.locked()
        await connection.close()

    asyncio.run(scenario())


def test_abort_session_fails_matching_pending_commands() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        connection = CdpConnection("ws://127.0.0.1/devtools", websocket=websocket)
        await connection.connect()

        aborted = asyncio.create_task(connection.call("Runtime.evaluate", session_id="session-1"))
        preserved = asyncio.create_task(connection.call("Runtime.evaluate", session_id="session-2"))
        await asyncio.sleep(0)

        connection.abort_session("session-1", "CDP 会话已断开：session-1")

        with pytest.raises(CdpDisconnectedError) as error:
            await aborted
        assert "CDP 会话已断开" in str(error.value)
        assert not preserved.done()

        await websocket.receive_payload({"id": websocket.sent[1]["id"], "result": {"ok": True}})
        assert await preserved == {"ok": True}
        await connection.close()

    asyncio.run(scenario())


def test_disconnect_fails_pending_commands() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        connection = CdpConnection("ws://127.0.0.1/devtools", websocket=websocket)
        await connection.connect()

        call_task = asyncio.create_task(connection.call("Browser.getVersion"))
        await asyncio.sleep(0)
        await websocket.queue.put(StopAsyncIteration)

        with pytest.raises(CdpDisconnectedError):
            await call_task
        await connection.close()

    asyncio.run(scenario())

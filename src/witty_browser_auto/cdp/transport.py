"""异步 CDP WebSocket JSON-RPC 传输。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.domain.errors import (
    CdpCommandError,
    CdpConnectionError,
    CdpDisconnectedError,
)

logger = logging.getLogger(__name__)
EventPredicate = Callable[[CdpEvent], bool]
EventHandler = Callable[[CdpEvent], Awaitable[None] | None]


def _safe_endpoint(websocket_url: str) -> str:
    """只保留来源，避免日志泄露可直接控制浏览器的 DevTools 路径。"""
    parts = urlsplit(websocket_url)
    hostname = parts.hostname or "<未知主机>"
    if ":" in hostname:
        hostname = f"[{hostname}]"
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        port = ""
    return f"{parts.scheme}://{hostname}{port}"


@dataclass(slots=True)
class _EventWaiter:
    future: asyncio.Future[CdpEvent]
    predicate: EventPredicate | None


@dataclass(slots=True)
class _PendingCommand:
    future: asyncio.Future[dict[str, Any]]
    method: str
    session_id: str | None


class CdpConnection:
    """在一个 WebSocket 上关联命令响应，并按 session 路由事件。"""

    def __init__(
        self,
        websocket_url: str,
        *,
        command_timeout_seconds: float = 15.0,
        http_session: aiohttp.ClientSession | None = None,
        websocket: Any | None = None,
    ) -> None:
        self.websocket_url = websocket_url
        self.command_timeout_seconds = command_timeout_seconds
        self._http_session = http_session
        self._owns_http_session = http_session is None
        self._websocket = websocket
        self._next_id = 0
        self._id_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._pending: dict[int, _PendingCommand] = {}
        self._event_waiters: dict[tuple[str | None, str], list[_EventWaiter]] = defaultdict(list)
        self._handlers: dict[tuple[str | None, str], list[EventHandler]] = defaultdict(list)
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    async def connect(self) -> None:
        if not self._closed:
            return
        if self._websocket is None:
            if self._http_session is None:
                self._http_session = aiohttp.ClientSession()
            try:
                self._websocket = await self._http_session.ws_connect(
                    self.websocket_url,
                    heartbeat=20,
                    max_msg_size=32 * 1024 * 1024,
                )
            except (aiohttp.ClientError, TimeoutError) as exc:
                await self._close_owned_session()
                raise CdpConnectionError(
                    "无法连接浏览器 CDP WebSocket",
                    context={"websocket_endpoint": _safe_endpoint(self.websocket_url)},
                ) from exc

        self._closed = False
        self._reader_task = asyncio.create_task(self._reader_loop(), name="witty-cdp-reader")
        logger.info(
            "CDP WebSocket 已连接",
            extra={"websocket_endpoint": _safe_endpoint(self.websocket_url)},
        )

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if self._closed or self._websocket is None:
            raise CdpDisconnectedError("CDP 连接尚未建立或已经断开")

        async with self._id_lock:
            self._next_id += 1
            command_id = self._next_id

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[command_id] = _PendingCommand(
            future=future,
            method=method,
            session_id=session_id,
        )
        payload: dict[str, Any] = {"id": command_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id

        timeout = timeout_seconds or self.command_timeout_seconds
        try:
            # 发送锁和 WebSocket 写入也必须计入命令预算。否则一个卡住的写入会占住
            # 全局发送锁，让后续页面诊断和鼠标动作永久等待且没有任何进度事件。
            async with asyncio.timeout(timeout):
                try:
                    async with self._send_lock:
                        await self._websocket.send_json(payload)
                except (aiohttp.ClientError, ConnectionError, RuntimeError) as exc:
                    self._pending.pop(command_id, None)
                    future.cancel()
                    raise CdpDisconnectedError(
                        "发送 CDP 命令时连接中断",
                        context={"method": method, "session_id": session_id},
                    ) from exc
                return await asyncio.shield(future)
        except TimeoutError as exc:
            self._pending.pop(command_id, None)
            future.cancel()
            raise CdpCommandError(
                f"CDP 命令执行超时：{method}",
                method=method,
                context={"session_id": session_id, "timeout_seconds": timeout},
            ) from exc
        except asyncio.CancelledError:
            self._pending.pop(command_id, None)
            future.cancel()
            raise

    def fail_session(self, session_id: str, error: Exception) -> None:
        """让指定 CDP Session 的未完成请求和事件等待立即失败。"""

        pending_ids = [
            command_id
            for command_id, pending in self._pending.items()
            if pending.session_id == session_id
        ]
        for command_id in pending_ids:
            pending = self._pending.pop(command_id, None)
            if pending and not pending.future.done():
                pending.future.set_exception(error)
        self._fail_event_waiters(error, session_id=session_id)
        if pending_ids:
            logger.warning(
                "CDP 会话已标记失败，未完成命令已终止",
                extra={"session_id": session_id, "pending_count": len(pending_ids)},
            )

    def abort_session(self, session_id: str, reason: str | None = None) -> None:
        """按 session 中止命令，供 Target.detachedFromTarget 等场景复用。"""

        message = reason or "CDP 会话已断开，当前页面命令已终止"
        self.fail_session(
            session_id,
            CdpDisconnectedError(message, context={"session_id": session_id}),
        )

    def subscribe(
        self,
        method: str,
        handler: EventHandler,
        *,
        session_id: str | None = None,
    ) -> Callable[[], None]:
        key = (session_id, method)
        self._handlers[key].append(handler)

        def unsubscribe() -> None:
            handlers = self._handlers.get(key)
            if handlers and handler in handlers:
                handlers.remove(handler)
            if handlers == []:
                self._handlers.pop(key, None)

        return unsubscribe

    @asynccontextmanager
    async def expect_event(
        self,
        method: str,
        *,
        session_id: str | None = None,
        predicate: EventPredicate | None = None,
    ) -> AsyncIterator[asyncio.Future[CdpEvent]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[CdpEvent] = loop.create_future()
        waiter = _EventWaiter(future=future, predicate=predicate)
        key = (session_id, method)
        self._event_waiters[key].append(waiter)
        try:
            yield future
        finally:
            waiters = self._event_waiters.get(key)
            if waiters and waiter in waiters:
                waiters.remove(waiter)
            if waiters == []:
                self._event_waiters.pop(key, None)
            if not future.done():
                future.cancel()

    async def wait_for_event(
        self,
        method: str,
        *,
        session_id: str | None = None,
        predicate: EventPredicate | None = None,
        timeout_seconds: float | None = None,
    ) -> CdpEvent:
        async with self.expect_event(method, session_id=session_id, predicate=predicate) as future:
            return await asyncio.wait_for(
                future,
                timeout=timeout_seconds or self.command_timeout_seconds,
            )

    async def close(self) -> None:
        if self._closed and self._reader_task is None:
            await self._close_owned_session()
            return

        self._closed = True
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            try:
                await websocket.close()
            except (aiohttp.ClientError, RuntimeError):
                logger.warning("关闭 CDP WebSocket 时出现异常")

        reader_task = self._reader_task
        self._reader_task = None
        if reader_task and reader_task is not asyncio.current_task():
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)

        self._fail_all(CdpDisconnectedError("CDP 连接已关闭"))
        await self._close_owned_session()
        logger.info("CDP WebSocket 已关闭")

    async def _reader_loop(self) -> None:
        disconnect_error: Exception = CdpDisconnectedError("CDP WebSocket 已断开")
        websocket = self._websocket
        if websocket is None:
            return
        try:
            async for message in websocket:
                payload = self._decode_message(message)
                if payload is None:
                    continue
                if "id" in payload:
                    self._handle_response(payload)
                elif "method" in payload:
                    self._dispatch_event(payload)
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, ConnectionError, json.JSONDecodeError, TypeError) as exc:
            disconnect_error = CdpDisconnectedError("读取 CDP 消息时连接中断")
            disconnect_error.__cause__ = exc
        finally:
            if not self._closed:
                self._closed = True
                self._fail_all(disconnect_error)
                logger.error("CDP 读取循环已停止，所有未完成命令均已失败")

    @staticmethod
    def _decode_message(message: Any) -> dict[str, Any] | None:
        if isinstance(message, dict):
            return message
        message_type = getattr(message, "type", None)
        if message_type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
            return None
        if message_type is aiohttp.WSMsgType.ERROR:
            raise CdpConnectionError("CDP WebSocket 返回错误消息")
        data = getattr(message, "data", message)
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        if not isinstance(data, str):
            return None
        decoded = json.loads(data)
        return decoded if isinstance(decoded, dict) else None

    def _handle_response(self, payload: dict[str, Any]) -> None:
        command_id = payload.get("id")
        if not isinstance(command_id, int):
            return
        pending = self._pending.pop(command_id, None)
        if pending is None or pending.future.done():
            return
        error = payload.get("error")
        if isinstance(error, dict):
            pending.future.set_exception(
                CdpCommandError(
                    str(error.get("message", "CDP 命令失败")),
                    method=pending.method,
                    error_code=error.get("code") if isinstance(error.get("code"), int) else None,
                    context={"data": error.get("data"), "session_id": pending.session_id},
                )
            )
            return
        result = payload.get("result")
        pending.future.set_result(result if isinstance(result, dict) else {})

    def _dispatch_event(self, payload: dict[str, Any]) -> None:
        method = payload.get("method")
        if not isinstance(method, str):
            return
        params = payload.get("params")
        event = CdpEvent(
            method=method,
            params=params if isinstance(params, dict) else {},
            session_id=payload.get("sessionId")
            if isinstance(payload.get("sessionId"), str)
            else None,
        )

        keys = {(event.session_id, method), (None, method)}
        for key in keys:
            for waiter in tuple(self._event_waiters.get(key, ())):
                if waiter.future.done():
                    continue
                if waiter.predicate is None or waiter.predicate(event):
                    waiter.future.set_result(event)
                    self._event_waiters[key].remove(waiter)

            for handler in tuple(self._handlers.get(key, ())):
                try:
                    result = handler(event)
                    if inspect.isawaitable(result):
                        handler_task = asyncio.create_task(self._await_handler(result))
                        handler_task.add_done_callback(self._log_handler_failure)
                except Exception:
                    logger.exception("处理 CDP 事件时发生异常", extra={"event_method": method})

    @staticmethod
    async def _await_handler(result: Awaitable[None]) -> None:
        await result

    @staticmethod
    def _log_handler_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error:
            logger.error(
                "异步 CDP 事件处理失败",
                exc_info=(type(error), error, error.__traceback__),
            )

    def _fail_all(self, error: Exception) -> None:
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(error)
        self._pending.clear()
        self._fail_event_waiters(error)

    def _fail_event_waiters(self, error: Exception, *, session_id: str | None = None) -> None:
        # 会话被浏览器主动摘除时，只清理对应 session 的等待，避免误伤其他 Target。
        keys = tuple(self._event_waiters.keys())
        for key in keys:
            waiter_session_id, _method = key
            if session_id is not None and waiter_session_id != session_id:
                continue
            waiters = self._event_waiters.pop(key, [])
            for waiter in waiters:
                if not waiter.future.done():
                    waiter.future.set_exception(error)

    async def _close_owned_session(self) -> None:
        if self._owns_http_session and self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

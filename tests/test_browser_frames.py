"""帧注册表：OOPIF 会话接入、坐标换算与帧清单。

坐标规则来自真实 Chrome 的探测结论：同进程 iframe 的盒模型在页面会话上已经是主框架
视口坐标，跨站 OOPIF 的盒模型则是帧内局部坐标，必须叠加宿主 iframe 的内容盒原点。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from witty_browser_auto.browser.frames import FrameRegistry
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.domain.errors import CdpCommandError, TargetNotFoundError
from witty_browser_auto.domain.models import BoundingBox

_MAIN_FRAME = "main-frame"
_SAME_PROCESS_FRAME = "same-frame"
_OOPIF_FRAME = "oopif-frame"


class FakeConnection:
    def __init__(self) -> None:
        self.handlers: dict[tuple[str | None, str], list[Any]] = {}
        self.calls: list[tuple[str | None, str, dict[str, Any]]] = []

    def subscribe(self, method: str, handler: Any, *, session_id: str | None = None) -> Any:
        self.handlers.setdefault((session_id, method), []).append(handler)

        def unsubscribe() -> None:
            self.handlers[(session_id, method)].remove(handler)

        return unsubscribe

    async def emit(self, method: str, params: dict[str, Any], *, session_id: str) -> None:
        for key in ((session_id, method), (None, method)):
            for handler in list(self.handlers.get(key, ())):
                result = handler(CdpEvent(method=method, params=params, session_id=session_id))
                if asyncio.iscoroutine(result):
                    await result

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append((session_id, method, params or {}))
        return _respond(session_id, method, params or {})


def _respond(session_id: str | None, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "Page.getFrameTree" and session_id == "oopif-session":
        return {
            "frameTree": {
                "frame": {
                    "id": _OOPIF_FRAME,
                    "parentId": _MAIN_FRAME,
                    "url": "http://cross.example/inner?token=secret",
                    "name": "支付框",
                }
            }
        }
    if method == "Page.getFrameTree":
        return {
            "frameTree": {
                "frame": {"id": _MAIN_FRAME, "url": "http://host.example/", "name": ""},
                "childFrames": [
                    {
                        "frame": {
                            "id": _SAME_PROCESS_FRAME,
                            "parentId": _MAIN_FRAME,
                            "url": "http://host.example/inner",
                            "name": "同源框",
                        }
                    }
                ],
            }
        }
    if method == "DOM.getFrameOwner":
        if params.get("frameId") not in {_SAME_PROCESS_FRAME, _OOPIF_FRAME}:
            raise AssertionError(f"未知 frameId：{params.get('frameId')}")
        return {"backendNodeId": 42}
    if method == "DOM.resolveNode":
        return {"object": {"objectId": "iframe-element"}}
    if method == "Page.createIsolatedWorld":
        return {"executionContextId": 7}
    if method == "Runtime.evaluate":
        if params.get("contextId") == 7:
            return {"result": {"objectId": "isolated-frame-document"}}
        return {"result": {"objectId": f"document-{session_id}"}}
    if method == "Runtime.callFunctionOn":
        declaration = str(params.get("functionDeclaration", ""))
        if "borderLeftWidth" in declaration:
            return {"result": {"value": {"x": 400.0, "y": 300.0}}}
        return {"result": {"objectId": "frame-document"}}
    return {}


class FakeSession:
    target_id = "page-target"
    session_id = "page-session"
    observation_version = 1

    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.frame_initialized = 0

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self.connection.call(method, params, session_id=self.session_id)

    async def initialize_frame(self) -> None:
        self.frame_initialized += 1


async def _registry_with_oopif() -> tuple[FrameRegistry, FakeConnection, FakeSession]:
    connection = FakeConnection()
    session = FakeSession(connection)
    registry = FrameRegistry(session)  # type: ignore[arg-type]
    await registry.start()
    await connection.emit(
        "Target.attachedToTarget",
        {
            "sessionId": "oopif-session",
            "targetInfo": {"type": "iframe", "targetId": _OOPIF_FRAME},
        },
        session_id=session.session_id,
    )
    return registry, connection, session


def test_registry_enables_auto_attach_on_the_page_session() -> None:
    """浏览器级 autoAttach 覆盖不到 OOPIF，必须在宿主页面会话上再声明一次。"""

    async def scenario() -> None:
        connection = FakeConnection()
        session = FakeSession(connection)
        registry = FrameRegistry(session)  # type: ignore[arg-type]
        await registry.start()

        auto_attach = [
            (target_session, params)
            for target_session, method, params in connection.calls
            if method == "Target.setAutoAttach"
        ]
        assert auto_attach == [
            ("page-session", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        ]

    asyncio.run(scenario())


def test_list_frames_merges_same_process_tree_with_attached_oopifs() -> None:
    """跨站 iframe 不在页面会话的帧树里，只能靠附着的独立会话补齐。"""

    async def scenario() -> None:
        registry, _connection, _session = await _registry_with_oopif()
        frames = {frame.frame_id: frame for frame in await registry.list_frames()}

        assert set(frames) == {_MAIN_FRAME, _SAME_PROCESS_FRAME, _OOPIF_FRAME}
        assert frames[_MAIN_FRAME].is_main is True
        assert frames[_SAME_PROCESS_FRAME].cross_origin is False
        assert frames[_SAME_PROCESS_FRAME].depth == 1
        oopif = frames[_OOPIF_FRAME]
        assert oopif.cross_origin is True
        assert oopif.parent_frame_id == _MAIN_FRAME
        assert oopif.depth == 1
        # 帧 URL 会进入模型上下文，查询串必须先脱敏。
        assert "secret" not in oopif.url

    asyncio.run(scenario())


def test_same_process_frame_runs_in_an_isolated_world_of_the_page_session() -> None:
    """同站但不同端口的 iframe 依然跨源，contentDocument 会是 null，只能用隔离世界。"""

    async def scenario() -> None:
        registry, connection, session = await _registry_with_oopif()
        handle = await registry.resolve(_SAME_PROCESS_FRAME)

        assert handle.session is session
        assert handle.cross_origin is False
        assert handle.document_object_id == "isolated-frame-document"
        worlds = [
            params
            for _target, method, params in connection.calls
            if method == "Page.createIsolatedWorld"
        ]
        assert worlds and worlds[0]["frameId"] == _SAME_PROCESS_FRAME

    asyncio.run(scenario())


@pytest.mark.parametrize("frame_id", [_SAME_PROCESS_FRAME, _OOPIF_FRAME])
def test_child_frames_report_owner_offset_for_viewport_translation(frame_id: str) -> None:
    """两类 iframe 的元素矩形都是帧内局部坐标，偏移换算规则必须一致。"""

    async def scenario() -> None:
        registry, _connection, _session = await _registry_with_oopif()
        handle = await registry.resolve(frame_id)

        assert (handle.offset_x, handle.offset_y) == (400.0, 300.0)
        moved = handle.to_viewport(BoundingBox(30, 40, 10, 10))
        assert (moved.x, moved.y) == (430.0, 340.0)
        assert (moved.width, moved.height) == (10, 10)

    asyncio.run(scenario())


def test_cross_origin_frame_resolves_to_its_own_session() -> None:
    async def scenario() -> None:
        registry, _connection, session = await _registry_with_oopif()
        handle = await registry.resolve(_OOPIF_FRAME)

        assert handle.session is not session
        assert handle.session.session_id == "oopif-session"
        assert handle.cross_origin is True
        assert handle.document_object_id == "document-oopif-session"

    asyncio.run(scenario())


def test_cross_origin_frame_session_is_initialized_once_and_narrowly() -> None:
    """OOPIF 与宿主页共享网络栈，只能开 DOM 与 Runtime，且重复解析不得重复 enable。"""

    async def scenario() -> None:
        registry, connection, _session = await _registry_with_oopif()
        await registry.resolve(_OOPIF_FRAME)
        await registry.resolve(_OOPIF_FRAME)

        enables = sorted(
            method
            for target_session, method, _params in connection.calls
            if target_session == "oopif-session" and method.endswith(".enable")
        )
        assert enables == ["DOM.enable", "Runtime.enable"]

    asyncio.run(scenario())


def test_main_frame_handle_avoids_repeating_frame_tree_lookups() -> None:
    """定位是热路径，主框架解析不能每次都重新问一遍帧树和文档句柄。"""

    async def scenario() -> None:
        registry, connection, _session = await _registry_with_oopif()
        first = await registry.resolve(None)
        before = len(connection.calls)
        second = await registry.resolve(None)

        assert first.is_main is True
        assert second.document_object_id == first.document_object_id
        assert len(connection.calls) == before

    asyncio.run(scenario())


def test_unknown_frame_id_is_rejected_with_actionable_message() -> None:
    async def scenario() -> None:
        registry, _connection, _session = await _registry_with_oopif()
        with pytest.raises(AssertionError):
            await registry.resolve("no-such-frame")

    asyncio.run(scenario())


def test_detached_frame_session_is_forgotten() -> None:
    async def scenario() -> None:
        registry, connection, session = await _registry_with_oopif()
        await connection.emit(
            "Target.detachedFromTarget",
            {"sessionId": "oopif-session"},
            session_id=session.session_id,
        )
        frames = {frame.frame_id for frame in await registry.list_frames()}

        assert _OOPIF_FRAME not in frames

    asyncio.run(scenario())


def test_stale_isolated_world_is_recreated_before_giving_up() -> None:
    """帧导航后旧的隔离世界会失效，重建一次比直接报错更接近调用方的预期。"""

    async def scenario() -> None:
        registry, connection, _session = await _registry_with_oopif()
        attempts = {"evaluate": 0}
        original = connection.call

        async def call(
            method: str,
            params: dict[str, Any] | None = None,
            *,
            session_id: str | None = None,
            timeout_seconds: float | None = None,
        ) -> dict[str, Any]:
            if method == "Runtime.evaluate" and (params or {}).get("contextId") == 7:
                attempts["evaluate"] += 1
                if attempts["evaluate"] == 1:
                    return {"result": {}}
            return await original(method, params, session_id=session_id)

        connection.call = call  # type: ignore[method-assign]
        handle = await registry.resolve(_SAME_PROCESS_FRAME)

        assert attempts["evaluate"] == 2
        assert handle.document_object_id == "isolated-frame-document"
        created = [
            method
            for _target, method, _p in connection.calls
            if method == "Page.createIsolatedWorld"
        ]
        assert len(created) == 2

    asyncio.run(scenario())


def test_missing_frame_owner_reports_the_frame_id() -> None:
    async def scenario() -> None:
        registry, connection, _session = await _registry_with_oopif()
        original = connection.call

        async def call(
            method: str,
            params: dict[str, Any] | None = None,
            *,
            session_id: str | None = None,
            timeout_seconds: float | None = None,
        ) -> dict[str, Any]:
            if method == "Page.createIsolatedWorld":
                raise CdpCommandError("No frame for given id found", method=method)
            return await original(method, params, session_id=session_id)

        connection.call = call  # type: ignore[method-assign]
        with pytest.raises(TargetNotFoundError, match=_SAME_PROCESS_FRAME):
            await registry.resolve(_SAME_PROCESS_FRAME)

    asyncio.run(scenario())

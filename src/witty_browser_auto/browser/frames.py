"""iframe 感知：帧发现、跨站 OOPIF 会话接入与视口坐标换算。

Chrome 把 iframe 分成两类，二者的协议表现完全不同，这个模块负责把差异收敛掉：

* 同站 iframe 与宿主同进程，落在页面 Target 里。`DOM.getBoxModel` 在页面会话上直接
  返回主框架视口坐标，因此偏移量为零；但脚本必须在该帧自己的 `document` 上执行，
  主文档的 `document.querySelector` 看不到帧内元素。
* 跨站 OOPIF 独占渲染进程，既不出现在页面会话的 `Page.getFrameTree` 里，也无法被
  `DOM.getDocument(pierce=true)` 穿透，必须在**页面会话**上再开一次 `Target.setAutoAttach`
  才会附着。它的 `DOM.getBoxModel` 返回帧内局部坐标，派发输入前要叠加祖先 iframe 的偏移。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.domain.errors import CdpCommandError, TargetNotFoundError
from witty_browser_auto.domain.models import BoundingBox
from witty_browser_auto.security.redaction import redact_url

logger = logging.getLogger(__name__)

_MAX_FRAMES = 100
_MAX_FRAME_DEPTH = 5

# 子帧视口原点等于 owner 元素的内容盒左上角，必须扣掉自身的边框与内边距。
_FRAME_CONTENT_ORIGIN_SCRIPT = r"""
function() {
  const rect = this.getBoundingClientRect();
  const style = getComputedStyle(this);
  const left = parseFloat(style.borderLeftWidth || '0') + parseFloat(style.paddingLeft || '0');
  const top = parseFloat(style.borderTopWidth || '0') + parseFloat(style.paddingTop || '0');
  return {x: rect.left + left, y: rect.top + top};
}
"""

_ISOLATED_WORLD_NAME = "witty_browser_auto_frame"


@dataclass(frozen=True, slots=True)
class FrameDescriptor:
    """对外可见的帧清单条目。"""

    frame_id: str
    parent_frame_id: str | None
    url: str
    name: str
    depth: int
    is_main: bool
    cross_origin: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "parent_frame_id": self.parent_frame_id,
            "url": self.url,
            "name": self.name,
            "depth": self.depth,
            "is_main": self.is_main,
            "cross_origin": self.cross_origin,
        }


@dataclass(frozen=True, slots=True)
class FrameHandle:
    """一个帧的执行面：在哪个会话上跑脚本，以及如何换算到视口坐标。"""

    frame_id: str
    session: CdpTargetSession
    document_object_id: str
    offset_x: float
    offset_y: float
    is_main: bool
    cross_origin: bool

    def to_viewport(self, box: BoundingBox) -> BoundingBox:
        """把帧内盒模型换算成主框架视口坐标，供输入派发使用。"""

        if not self.offset_x and not self.offset_y:
            return box
        return BoundingBox(box.x + self.offset_x, box.y + self.offset_y, box.width, box.height)

    async def call_on_document(
        self,
        declaration: str,
        arguments: list[dict[str, Any]] | None = None,
        *,
        return_by_value: bool = True,
    ) -> dict[str, Any]:
        """在本帧的 document 上执行固定脚本模板。"""

        return await self.session.call(
            "Runtime.callFunctionOn",
            {
                "objectId": self.document_object_id,
                "functionDeclaration": declaration,
                "arguments": arguments or [],
                "returnByValue": return_by_value,
            },
        )


class FrameRegistry:
    """按页面会话维护帧清单与 OOPIF 子会话。"""

    def __init__(self, session: CdpTargetSession) -> None:
        self._session = session
        self._connection = session.connection
        self._oopif_sessions: dict[str, CdpTargetSession] = {}
        self._initialized_sessions: set[str] = set()
        self._unsubscribers: list[Any] = []
        self._started = False
        self._main_frame_id: str | None = None
        self._main_document: tuple[int, str] | None = None
        self._isolated_contexts: dict[str, tuple[int, int]] = {}

    @property
    def page_session(self) -> CdpTargetSession:
        return self._session

    async def start(self) -> None:
        """在页面会话上开启 OOPIF 自动附着。

        浏览器级的 `Target.setAutoAttach` 只覆盖 page/worker 一层，跨站 iframe 必须在其
        宿主页面会话上再声明一次，否则连 attach 事件都收不到。
        """

        if self._started:
            return
        self._unsubscribers.append(
            self._connection.subscribe(
                "Target.attachedToTarget",
                self._on_attached,
                session_id=self._session.session_id,
            )
        )
        self._unsubscribers.append(
            self._connection.subscribe(
                "Target.detachedFromTarget",
                self._on_detached,
                session_id=self._session.session_id,
            )
        )
        await self._session.call(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        self._started = True

    def close(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        self._oopif_sessions.clear()
        self._initialized_sessions.clear()
        self._main_document = None
        self._isolated_contexts.clear()
        self._started = False

    async def list_frames(self) -> list[FrameDescriptor]:
        """列出主框架及其所有可见子帧，URL 统一脱敏。"""

        tree = await self._session.call("Page.getFrameTree")
        root = tree.get("frameTree")
        if not isinstance(root, dict):
            raise TargetNotFoundError("浏览器没有返回页面帧树")
        descriptors: list[FrameDescriptor] = []
        self._collect_same_process(root, depth=0, descriptors=descriptors)
        known = {item.frame_id for item in descriptors}
        for frame_id in tuple(self._oopif_sessions):
            if frame_id in known or len(descriptors) >= _MAX_FRAMES:
                continue
            descriptor = await self._describe_oopif(frame_id, descriptors)
            if descriptor is not None:
                descriptors.append(descriptor)
        return descriptors

    async def resolve(self, frame_id: str | None) -> FrameHandle:
        """把 frame_id 解析成可执行的帧句柄；空值表示主框架。"""

        main_frame_id = await self._resolve_main_frame_id()
        if not frame_id or frame_id == main_frame_id:
            return FrameHandle(
                frame_id=main_frame_id,
                session=self._session,
                document_object_id=await self._main_document_object_id(),
                offset_x=0.0,
                offset_y=0.0,
                is_main=True,
                cross_origin=False,
            )
        offset_x, offset_y = await self._frame_origin(frame_id)
        oopif = self._oopif_sessions.get(frame_id)
        if oopif is not None:
            await self._ensure_frame_session(oopif)
            return FrameHandle(
                frame_id=frame_id,
                session=oopif,
                document_object_id=await self._document_object_id(oopif),
                offset_x=offset_x,
                offset_y=offset_y,
                is_main=False,
                cross_origin=True,
            )
        return FrameHandle(
            frame_id=frame_id,
            session=self._session,
            document_object_id=await self._same_process_document(frame_id),
            offset_x=offset_x,
            offset_y=offset_y,
            is_main=False,
            cross_origin=False,
        )

    async def _resolve_main_frame_id(self) -> str:
        """主框架 ID 在页面 Target 的整个生命周期内不变，取一次即可。"""

        if self._main_frame_id is None:
            tree = await self._session.call("Page.getFrameTree")
            frame_id = tree.get("frameTree", {}).get("frame", {}).get("id")
            if not isinstance(frame_id, str):
                raise TargetNotFoundError("浏览器没有返回主框架 ID")
            self._main_frame_id = frame_id
        return self._main_frame_id

    async def _main_document_object_id(self) -> str:
        """按观察版本缓存主文档句柄；导航或文档替换会让旧句柄失效。"""

        version = self._session.observation_version
        cached = self._main_document
        if cached is not None and cached[0] == version:
            return cached[1]
        object_id = await self._document_object_id(self._session)
        self._main_document = (version, object_id)
        return object_id

    def _collect_same_process(
        self,
        node: dict[str, Any],
        *,
        depth: int,
        descriptors: list[FrameDescriptor],
    ) -> None:
        frame = node.get("frame")
        if not isinstance(frame, dict) or len(descriptors) >= _MAX_FRAMES:
            return
        frame_id = frame.get("id")
        if not isinstance(frame_id, str):
            return
        descriptors.append(
            FrameDescriptor(
                frame_id=frame_id,
                parent_frame_id=frame.get("parentId") if depth else None,
                url=redact_url(str(frame.get("url", ""))),
                name=str(frame.get("name", ""))[:200],
                depth=depth,
                is_main=depth == 0,
                cross_origin=False,
            )
        )
        if depth >= _MAX_FRAME_DEPTH:
            return
        for child in node.get("childFrames", []) or []:
            if isinstance(child, dict):
                self._collect_same_process(child, depth=depth + 1, descriptors=descriptors)

    async def _describe_oopif(
        self,
        frame_id: str,
        known: list[FrameDescriptor],
    ) -> FrameDescriptor | None:
        session = self._oopif_sessions.get(frame_id)
        if session is None:
            return None
        try:
            tree = await session.call("Page.getFrameTree")
        except CdpCommandError:
            return None
        frame = tree.get("frameTree", {}).get("frame", {})
        parent_id = frame.get("parentId")
        depths = {item.frame_id: item.depth for item in known}
        return FrameDescriptor(
            frame_id=frame_id,
            parent_frame_id=parent_id if isinstance(parent_id, str) else None,
            url=redact_url(str(frame.get("url", ""))),
            name=str(frame.get("name", ""))[:200],
            depth=depths.get(str(parent_id), 0) + 1,
            is_main=False,
            cross_origin=True,
        )

    async def _frame_origin(self, frame_id: str) -> tuple[float, float]:
        """累加祖先 iframe 的内容盒原点，得到该帧视口原点在主框架中的位置。

        每一层的 owner 元素都在父帧的坐标系里测量，所以必须沿链累加，不能只取直接父级。
        """

        offset_x = 0.0
        offset_y = 0.0
        current = frame_id
        for _ in range(_MAX_FRAME_DEPTH + 1):
            parent_id = await self._parent_frame_id(current)
            parent_session = self._oopif_sessions.get(parent_id or "", self._session)
            origin = await self._frame_owner_origin(parent_session, current)
            offset_x += origin[0]
            offset_y += origin[1]
            if parent_id is None or parent_id == self._main_frame_id:
                return offset_x, offset_y
            current = parent_id
        raise TargetNotFoundError("iframe 嵌套层级超出支持范围")

    async def _parent_frame_id(self, frame_id: str) -> str | None:
        session = self._oopif_sessions.get(frame_id)
        if session is not None:
            tree = await session.call("Page.getFrameTree")
            parent_id = tree.get("frameTree", {}).get("frame", {}).get("parentId")
            return parent_id if isinstance(parent_id, str) else None
        for descriptor in await self.list_frames():
            if descriptor.frame_id == frame_id:
                return descriptor.parent_frame_id
        return None

    async def _frame_owner_origin(
        self,
        session: CdpTargetSession,
        frame_id: str,
    ) -> tuple[float, float]:
        object_id = await self._frame_owner_object(session, frame_id)
        result = await session.call(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": _FRAME_CONTENT_ORIGIN_SCRIPT,
                "returnByValue": True,
            },
        )
        value = result.get("result", {}).get("value")
        if not isinstance(value, dict):
            raise TargetNotFoundError("无法读取 iframe 在父文档中的位置")
        return float(value.get("x", 0.0)), float(value.get("y", 0.0))

    async def _frame_owner_object(self, session: CdpTargetSession, frame_id: str) -> str:
        try:
            owner = await session.call("DOM.getFrameOwner", {"frameId": frame_id})
        except CdpCommandError as exc:
            raise TargetNotFoundError(f"页面上找不到 frame_id 为 {frame_id} 的 iframe") from exc
        backend_node_id = owner.get("backendNodeId")
        if not isinstance(backend_node_id, int):
            raise TargetNotFoundError("iframe 宿主节点无法解析")
        resolved = await session.call("DOM.resolveNode", {"backendNodeId": backend_node_id})
        object_id = resolved.get("object", {}).get("objectId")
        if not isinstance(object_id, str):
            raise TargetNotFoundError("iframe 宿主节点无法解析为远程对象")
        return object_id

    async def _same_process_document(self, frame_id: str) -> str:
        """同进程 iframe 没有独立会话，用隔离世界拿到它的 document。

        不能走宿主元素的 `contentDocument`：同站但不同端口的 iframe 依然受同源策略限制，
        `contentDocument` 会是 null。隔离世界工作在同源策略之下，对任何同进程帧都可用，
        而且不需要等待 `Runtime.executionContextCreated` 事件，没有附着时序竞争。
        """

        for attempt in range(2):
            context_id = await self._isolated_context(frame_id, refresh=attempt > 0)
            result = await self._session.call(
                "Runtime.evaluate",
                {"expression": "document", "returnByValue": False, "contextId": context_id},
            )
            object_id = result.get("result", {}).get("objectId")
            if isinstance(object_id, str):
                return object_id
        raise TargetNotFoundError(f"frame_id 为 {frame_id} 的 iframe 文档不可访问")

    async def _isolated_context(self, frame_id: str, *, refresh: bool) -> int:
        """按观察版本缓存隔离世界；每次新建都会在浏览器里多留一个世界。"""

        version = self._session.observation_version
        cached = self._isolated_contexts.get(frame_id)
        if not refresh and cached is not None and cached[0] == version:
            return cached[1]
        try:
            created = await self._session.call(
                "Page.createIsolatedWorld",
                {"frameId": frame_id, "worldName": _ISOLATED_WORLD_NAME},
            )
        except CdpCommandError as exc:
            raise TargetNotFoundError(f"页面上找不到 frame_id 为 {frame_id} 的 iframe") from exc
        context_id = created.get("executionContextId")
        if not isinstance(context_id, int):
            raise TargetNotFoundError(f"frame_id 为 {frame_id} 的 iframe 无法建立执行上下文")
        self._isolated_contexts[frame_id] = (version, context_id)
        return context_id

    async def _document_object_id(self, session: CdpTargetSession) -> str:
        result = await session.call(
            "Runtime.evaluate",
            {"expression": "document", "returnByValue": False},
        )
        object_id = result.get("result", {}).get("objectId")
        if not isinstance(object_id, str):
            raise TargetNotFoundError("无法获取帧文档对象")
        return object_id

    async def _ensure_frame_session(self, session: CdpTargetSession) -> None:
        if session.session_id in self._initialized_sessions:
            return
        await session.initialize_frame()
        self._initialized_sessions.add(session.session_id)

    async def _on_attached(self, event: CdpEvent) -> None:
        target_info = event.params.get("targetInfo")
        session_id = event.params.get("sessionId")
        if not isinstance(target_info, dict) or not isinstance(session_id, str):
            return
        if target_info.get("type") != "iframe":
            return
        target_id = target_info.get("targetId")
        if not isinstance(target_id, str):
            return
        # OOPIF 的 targetId 与其 frameId 相同，这是关联父文档 iframe 元素的唯一钥匙。
        self._oopif_sessions[target_id] = CdpTargetSession(
            self._connection,
            target_id,
            session_id,
            "iframe",
        )
        logger.debug("已附着跨站 iframe 会话", extra={"frame_id": target_id})

    def _on_detached(self, event: CdpEvent) -> None:
        session_id = event.params.get("sessionId")
        if not isinstance(session_id, str):
            return
        for frame_id, session in tuple(self._oopif_sessions.items()):
            if session.session_id == session_id:
                self._oopif_sessions.pop(frame_id, None)
                self._initialized_sessions.discard(session_id)


async def wait_for_frames(
    registry: FrameRegistry,
    *,
    minimum: int = 2,
    timeout_seconds: float = 3.0,
) -> list[FrameDescriptor]:
    """等待子帧就绪；跨站 iframe 的附着事件晚于 load，直接列举容易漏掉。"""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    frames = await registry.list_frames()
    while len(frames) < minimum and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        frames = await registry.list_frames()
    return frames

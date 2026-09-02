"""浏览器、BrowserContext 与 Target Session 生命周期。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from witty_browser_auto.browser.dialogs import DialogSupervisor
from witty_browser_auto.browser.launcher import ChromiumLauncher, ManagedBrowserProcess
from witty_browser_auto.browser.live_browser import (
    discover_live_browser_endpoint,
    open_live_browser_authorization_page,
    wait_for_live_browser_endpoint,
)
from witty_browser_auto.cdp.discovery import discover_devtools_endpoint
from witty_browser_auto.cdp.protocol import BrowserVersion, CdpEvent
from witty_browser_auto.cdp.transport import CdpConnection
from witty_browser_auto.config import BrowserConfig, BrowserSessionMode
from witty_browser_auto.domain.errors import CdpCommandError, CdpConnectionError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CdpTargetSession:
    connection: CdpConnection
    target_id: str
    session_id: str
    target_type: str = "page"
    observation_version: int = 1
    dialog_supervisor: DialogSupervisor | None = None
    _unsubscribers: list[Any] = field(default_factory=list)

    async def initialize(self) -> None:
        await asyncio.gather(
            self.call("Page.enable"),
            self.call("Runtime.enable"),
            self.call("DOM.enable"),
            self.call("Accessibility.enable"),
            self.call("Network.enable", {"maxTotalBufferSize": 10_000_000}),
        )
        for method in (
            "DOM.documentUpdated",
            "Page.frameNavigated",
            "Runtime.executionContextsCleared",
        ):
            self._unsubscribers.append(
                self.connection.subscribe(method, self._mark_changed, session_id=self.session_id)
            )
        # 启用 Page 域就必须应答对话框，否则渲染进程会一直挂起。
        self._unsubscribers.append(
            self.connection.subscribe(
                "Page.javascriptDialogOpening",
                self._on_dialog,
                session_id=self.session_id,
            )
        )

    async def initialize_frame(self) -> None:
        """OOPIF 子会话只开 DOM 与 Runtime。

        它与宿主页面共享网络栈与可访问性树，重复 enable 会让网络记录和观察出现同一
        请求的双份事件。
        """

        await asyncio.gather(self.call("Runtime.enable"), self.call("DOM.enable"))

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self.connection.call(
            method,
            params,
            session_id=self.session_id,
            timeout_seconds=timeout_seconds,
        )

    def close(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()

    def _mark_changed(self, event: CdpEvent) -> None:
        self.observation_version += 1

    async def _on_dialog(self, event: CdpEvent) -> None:
        supervisor = self.dialog_supervisor
        if supervisor is None:
            # 没有接管者时也必须回答，否则页面就此挂死；取消是不可逆性最低的选择。
            await self.call("Page.handleJavaScriptDialog", {"accept": False}, timeout_seconds=5)
            return
        await supervisor.handle_event(self, event.params)


class CdpBrowser:
    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self.dialog_supervisor = DialogSupervisor()
        self.http_session: aiohttp.ClientSession | None = None
        self.connection: CdpConnection | None = None
        self.managed_process: ManagedBrowserProcess | None = None
        self.version: BrowserVersion | None = None
        self._sessions_by_target: dict[str, CdpTargetSession] = {}
        self._sessions_by_id: dict[str, CdpTargetSession] = {}
        self._unsubscribers: list[Any] = []
        self._external = bool(config.cdp_endpoint)
        self.reattached = False
        self._reattach_target_id: str | None = None
        self._launcher: ChromiumLauncher | None = None

    async def start(self) -> None:
        if self.connection and not self.connection.closed:
            return
        endpoint = self.config.cdp_endpoint
        if not endpoint:
            if self.config.session_mode is BrowserSessionMode.TAKEOVER:
                endpoint = await discover_live_browser_endpoint()
                if endpoint is None:
                    authorization_page_opened = await open_live_browser_authorization_page()
                    if authorization_page_opened:
                        endpoint = await wait_for_live_browser_endpoint(timeout_seconds=60.0)
                if endpoint is None:
                    raise CdpConnectionError(
                        "已在当前 Chrome 打开原生接管授权页；请开启 Remote debugging，"
                        "并在连接提示中选择 Allow"
                        if authorization_page_opened
                        else "未检测到可接管的运行中 Chrome；请先打开 Chrome"
                    )
                self._external = True
                self.reattached = True
            else:
                self._launcher = ChromiumLauncher(self.config)
                existing = await self._launcher.find_existing_endpoint()
                if existing is not None:
                    endpoint = existing.endpoint
                    self._external = True
                    self.reattached = True
                    self._reattach_target_id = existing.target_id
                else:
                    self.managed_process = await self._launcher.launch()
                    endpoint = self.managed_process.endpoint
                    self._external = False

        try:
            self.http_session = aiohttp.ClientSession()
            discovered = await discover_devtools_endpoint(
                endpoint,
                http_session=self.http_session,
                timeout_seconds=self.config.launch_timeout_seconds,
            )
            self.connection = CdpConnection(
                discovered.websocket_url,
                command_timeout_seconds=self.config.command_timeout_seconds,
                http_session=self.http_session,
            )
            await self.connection.connect()
            version_result = await self.connection.call("Browser.getVersion")
            self.version = BrowserVersion.from_result(version_result)
            self._unsubscribers.extend(
                [
                    self.connection.subscribe("Target.attachedToTarget", self._on_attached),
                    self.connection.subscribe("Target.detachedFromTarget", self._on_detached),
                    self.connection.subscribe("Target.targetDestroyed", self._on_destroyed),
                ]
            )
            await self.connection.call("Target.setDiscoverTargets", {"discover": True})
            await self.connection.call(
                "Target.setAutoAttach",
                {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": False,
                    "flatten": True,
                },
            )
        except Exception:
            await self.close()
            raise
        logger.info(
            "CDP 浏览器会话已就绪",
            extra={
                "product": self.version.product,
                "protocol_version": self.version.protocol_version,
                "external": self._external,
                "reattached": self.reattached,
            },
        )

    async def claim_existing_page(self) -> CdpTargetSession | None:
        connection = self._require_connection()
        result = await connection.call("Target.getTargets")
        raw_targets = result.get("targetInfos")
        if not isinstance(raw_targets, list):
            return None
        page_targets = [
            target
            for target in raw_targets
            if isinstance(target, dict)
            and target.get("type") == "page"
            and isinstance(target.get("targetId"), str)
            and isinstance(target.get("url"), str)
            and target.get("url") != "about:blank"
        ]
        preferred = next(
            (
                target
                for target in page_targets
                if target.get("targetId") == self._reattach_target_id
            ),
            None,
        )
        if preferred is None and page_targets:
            focus_ranks = await asyncio.gather(
                *(self._page_focus_rank(str(target["targetId"])) for target in page_targets)
            )
            preferred = page_targets[max(range(len(page_targets)), key=focus_ranks.__getitem__)]
        if preferred is None:
            return None
        target_id = str(preferred["targetId"])
        session = self._sessions_by_target.get(target_id)
        if session is None:
            attach_result = await connection.call(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
            session_id = attach_result.get("sessionId")
            if not isinstance(session_id, str):
                raise CdpConnectionError("现有页面未返回 Target Session ID")
            session = CdpTargetSession(connection, target_id, session_id)
            self._register_session(session)
        await session.initialize()
        return session

    async def _page_focus_rank(self, target_id: str) -> int:
        connection = self._require_connection()
        session = self._sessions_by_target.get(target_id)
        if session is None:
            try:
                attach_result = await connection.call(
                    "Target.attachToTarget",
                    {"targetId": target_id, "flatten": True},
                )
            except CdpCommandError:
                return 0
            session_id = attach_result.get("sessionId")
            if not isinstance(session_id, str):
                return 0
            session = CdpTargetSession(connection, target_id, session_id)
            self._register_session(session)
        try:
            result = await session.call(
                "Runtime.evaluate",
                {
                    "expression": (
                        "({focused:document.hasFocus(),visibility:document.visibilityState})"
                    ),
                    "returnByValue": True,
                },
                timeout_seconds=0.5,
            )
        except (CdpCommandError, TimeoutError):
            return 0
        value = result.get("result", {}).get("value")
        if not isinstance(value, dict):
            return 0
        if value.get("focused") is True:
            return 2
        return 1 if value.get("visibility") == "visible" else 0

    def remember_target(self, target_id: str) -> None:
        if self._launcher is not None:
            self._launcher.remember_target(target_id)

    async def create_context(self) -> str:
        connection = self._require_connection()
        result = await connection.call("Target.createBrowserContext", {"disposeOnDetach": True})
        context_id = result.get("browserContextId")
        if not isinstance(context_id, str):
            raise CdpConnectionError("浏览器未返回 BrowserContext ID")
        return context_id

    async def create_page(
        self,
        context_id: str | None,
        url: str = "about:blank",
        *,
        new_window: bool = False,
    ) -> CdpTargetSession:
        connection = self._require_connection()
        result = await self._create_target(context_id, url, new_window=new_window)
        target_id = result.get("targetId")
        if not isinstance(target_id, str):
            raise CdpConnectionError("浏览器未返回 Target ID")

        # autoAttach 事件通常先到；短暂等待可避免为同一 Target 建立重复 session。
        deadline = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < deadline:
            session = self._sessions_by_target.get(target_id)
            if session:
                await session.initialize()
                return session
            await asyncio.sleep(0.01)

        attach_result = await connection.call(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        session_id = attach_result.get("sessionId")
        if not isinstance(session_id, str):
            raise CdpConnectionError("浏览器未返回 Target Session ID")
        session = CdpTargetSession(connection, target_id, session_id)
        self._register_session(session)
        await session.initialize()
        return session

    async def create_window(
        self,
        context_id: str | None,
        url: str = "about:blank",
    ) -> CdpTargetSession:
        """在当前已连接浏览器中新建独立窗口并返回其页面会话。"""

        return await self.create_page(context_id, url, new_window=True)

    async def list_page_targets(self) -> list[dict[str, Any]]:
        """列出浏览器当前全部页面 Target 的原始元数据。"""

        connection = self._require_connection()
        result = await connection.call("Target.getTargets")
        raw_targets = result.get("targetInfos")
        if not isinstance(raw_targets, list):
            return []
        return [
            {
                "target_id": str(target["targetId"]),
                "url": str(target.get("url", "")),
                "title": str(target.get("title", "")),
                "attached": bool(target.get("attached", False)),
            }
            for target in raw_targets
            if isinstance(target, dict)
            and target.get("type") == "page"
            and isinstance(target.get("targetId"), str)
        ]

    async def attach_to_page(self, target_id: str) -> CdpTargetSession:
        """附加到指定页面 Target 并初始化协议域；已附加时复用现有会话。"""

        connection = self._require_connection()
        session = self._sessions_by_target.get(target_id)
        if session is None:
            attach_result = await connection.call(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
            session_id = attach_result.get("sessionId")
            if not isinstance(session_id, str):
                raise CdpConnectionError("页面未返回 Target Session ID")
            session = CdpTargetSession(connection, target_id, session_id)
            self._register_session(session)
        await session.initialize()
        return session

    async def wait_for_target_session(
        self,
        target_id: str,
        *,
        timeout_seconds: float = 3.0,
    ) -> CdpTargetSession:
        """等待 autoAttach 注册指定页面，并在交给驱动前初始化协议域。"""

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            session = self._sessions_by_target.get(target_id)
            if session is not None:
                await session.initialize()
                return session
            await asyncio.sleep(0.01)
        raise CdpConnectionError(
            "新标签页已创建，但未能建立 CDP Target Session",
            context={"target_id": target_id, "timeout_seconds": timeout_seconds},
        )

    def is_session_active(self, session: CdpTargetSession) -> bool:
        connection = self.connection
        return bool(
            connection
            and not connection.closed
            and self._sessions_by_target.get(session.target_id) is session
            and self._sessions_by_id.get(session.session_id) is session
        )

    async def _create_target(
        self,
        context_id: str | None,
        url: str,
        *,
        new_window: bool = False,
    ) -> dict[str, Any]:
        connection = self._require_connection()
        create_params: dict[str, Any] = {"url": url}
        if context_id:
            create_params["browserContextId"] = context_id
        if new_window:
            create_params["newWindow"] = True
        try:
            result = await connection.call("Target.createTarget", create_params)
        except CdpCommandError as exc:
            if new_window or "no browser is open" not in str(exc).lower():
                raise
            # 新版无头 Chrome 可能没有可承载普通 tab 的窗口，此时显式创建窗口。
            logger.info("无头浏览器没有现有窗口，改用新窗口创建页面 Target")
            result = await connection.call(
                "Target.createTarget",
                {**create_params, "newWindow": True},
            )
        return result

    async def close_target(self, target_id: str) -> None:
        connection = self._require_connection()
        await connection.call("Target.closeTarget", {"targetId": target_id})
        session = self._sessions_by_target.pop(target_id, None)
        if session:
            self._sessions_by_id.pop(session.session_id, None)
            session.close()

    async def dispose_context(self, context_id: str) -> None:
        await self._require_connection().call(
            "Target.disposeBrowserContext",
            {"browserContextId": context_id},
        )

    async def close(self) -> None:
        for session in tuple(self._sessions_by_id.values()):
            session.close()
        self._sessions_by_id.clear()
        self._sessions_by_target.clear()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()

        connection = self.connection
        self.connection = None
        if connection:
            if self.managed_process is not None and not connection.closed:
                try:
                    await connection.call("Browser.close", timeout_seconds=3)
                except Exception:
                    logger.warning("通过 CDP 关闭受管浏览器失败，将终止本项目拥有的进程")
            await connection.close()
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        self.http_session = None
        if self.managed_process:
            await self.managed_process.terminate()
            self.managed_process = None
        self.reattached = False
        self._reattach_target_id = None

    async def _on_attached(self, event: CdpEvent) -> None:
        session_id = event.params.get("sessionId")
        target_info = event.params.get("targetInfo")
        if not isinstance(session_id, str) or not isinstance(target_info, dict):
            return
        target_id = target_info.get("targetId")
        target_type = target_info.get("type", "")
        if not isinstance(target_id, str) or target_type not in {"page", "iframe"}:
            return
        self._register_session(
            CdpTargetSession(
                self._require_connection(),
                target_id,
                session_id,
                str(target_type),
            )
        )

    def _on_detached(self, event: CdpEvent) -> None:
        session_id = event.params.get("sessionId")
        if not isinstance(session_id, str):
            return
        connection = self.connection
        if connection and not connection.closed:
            # detached 事件意味着浏览器已主动摘除该 Target Session，必须立刻让挂起命令失败。
            connection.abort_session(session_id, f"CDP 会话已断开：{session_id}")
        session = self._sessions_by_id.pop(session_id, None)
        if session:
            self._sessions_by_target.pop(session.target_id, None)
            session.close()

    def _on_destroyed(self, event: CdpEvent) -> None:
        target_id = event.params.get("targetId")
        if not isinstance(target_id, str):
            return
        session = self._sessions_by_target.pop(target_id, None)
        if session:
            connection = self.connection
            if connection and not connection.closed:
                connection.abort_session(
                    session.session_id,
                    f"页面 Target 已销毁：{target_id}",
                )
            self._sessions_by_id.pop(session.session_id, None)
            session.close()

    def _register_session(self, session: CdpTargetSession) -> None:
        # 所有会话都在这里登记，因此这是唯一需要注入对话框接管者的地方。
        session.dialog_supervisor = self.dialog_supervisor
        previous = self._sessions_by_target.get(session.target_id)
        if previous and previous.session_id != session.session_id:
            previous.close()
            self._sessions_by_id.pop(previous.session_id, None)
        self._sessions_by_target[session.target_id] = session
        self._sessions_by_id[session.session_id] = session

    def _require_connection(self) -> CdpConnection:
        if not self.connection or self.connection.closed:
            raise CdpConnectionError("CDP 浏览器尚未启动或连接已经断开")
        return self.connection

"""MCP 服务端持有的浏览器会话。

Python 调用方用 `async with launch_browser_toolkit(...)` 管生命周期；MCP 客户端做不到，
只能靠两次工具调用。这里把装配、记忆运行时启停与驱动关闭收敛成一处，保证任何路径下
`close` 都会把浏览器与后台写入队列一起收干净。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from witty_browser_auto.config import AppConfig
from witty_browser_auto.toolkit.bootstrap import build_browser_toolkit
from witty_browser_auto.toolkit.facade import BrowserToolkit

logger = logging.getLogger(__name__)


class SessionNotOpenError(RuntimeError):
    """在没有浏览器会话时调用了页面工具。"""


class ToolkitSession:
    """同一时间只保持一个浏览器会话；重复 open 会先把旧会话收干净。"""

    def __init__(
        self,
        *,
        config: AppConfig | None = None,
        inputs: Mapping[str, Any] | None = None,
        allowed_origins: Sequence[str] = (),
        project_id: str = "mcp",
        allow_visual_actions: bool = False,
        respect_robots: bool = False,
        min_request_interval_ms: float = 0.0,
        read_only: bool = False,
    ) -> None:
        self._config = config
        self._inputs = dict(inputs or {})
        self._allowed_origins = tuple(allowed_origins)
        self._project_id = project_id
        self._allow_visual_actions = allow_visual_actions
        self._respect_robots = respect_robots
        self._min_request_interval_ms = min_request_interval_ms
        self._read_only = read_only
        self._toolkit: BrowserToolkit | None = None
        self._driver: Any = None

    @property
    def toolkit(self) -> BrowserToolkit | None:
        return self._toolkit

    @property
    def input_keys(self) -> tuple[str, ...]:
        """只暴露键名：值由执行层在最后一刻解析，不进任何返回结构。"""

        return tuple(sorted(self._inputs))

    def require(self) -> BrowserToolkit:
        if self._toolkit is None:
            raise SessionNotOpenError("还没有浏览器会话，请先调用 open_browser")
        return self._toolkit

    async def open(self, url: str) -> dict[str, Any]:
        if self._toolkit is not None:
            await self.close()
        toolkit, driver = build_browser_toolkit(
            url,
            goal="MCP 客户端浏览器会话",
            config=self._config,
            inputs=self._inputs,
            allowed_origins=self._allowed_origins or None,
            project_id=self._project_id,
            allow_visual_actions=self._allow_visual_actions,
            respect_robots=self._respect_robots,
            min_request_interval_ms=self._min_request_interval_ms,
            read_only=self._read_only,
        )
        if toolkit.memory_runtime is not None:
            toolkit.memory_runtime.start()
        try:
            surface_id = await toolkit.open(url)
        except Exception:
            # 打开失败也要把已经拉起的浏览器收掉，否则会留下孤儿进程。
            await self._shutdown(toolkit, driver)
            raise
        self._toolkit = toolkit
        self._driver = driver
        return {
            "surface_id": surface_id,
            "url": url,
            "task_id": toolkit.task.task_id,
            "allowed_origins": list(toolkit.task.scope.allowed_origins),
            "available_input_keys": list(self.input_keys),
            "allow_visual_actions": self._allow_visual_actions,
            "respect_robots": self._respect_robots,
            "min_request_interval_ms": self._min_request_interval_ms,
            "read_only": toolkit.task.read_only,
        }

    async def close(self) -> dict[str, Any]:
        toolkit, driver = self._toolkit, self._driver
        self._toolkit = None
        self._driver = None
        if toolkit is None:
            return {"closed": False, "reason": "当前没有浏览器会话"}
        await self._shutdown(toolkit, driver)
        return {"closed": True}

    @staticmethod
    async def _shutdown(toolkit: BrowserToolkit, driver: Any) -> None:
        if toolkit.memory_runtime is not None:
            try:
                await toolkit.memory_runtime.close(timeout_seconds=5.0)
            except Exception:
                logger.warning("关闭记忆运行时失败，继续关闭浏览器", exc_info=True)
        if driver is not None:
            await driver.close()

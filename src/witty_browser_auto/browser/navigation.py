"""页面导航完成信号的等待原语。

前进/后退会命中浏览器的前进后退缓存 bfcache，此时 Chrome 只发 `Page.frameNavigated`
并带上 `type=BackForwardCacheRestore`，不会重新发 `Page.loadEventFired`，只等加载事件
必然超时。这里把"加载完成"与"缓存恢复"合并成同一个等待条件。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.cdp.protocol import CdpEvent

LOAD_EVENT = "load_event"
BACK_FORWARD_CACHE_RESTORE = "back_forward_cache_restore"


def _is_back_forward_cache_restore(event: CdpEvent) -> bool:
    """只认主框架的缓存恢复；子框架恢复不代表调用方等待的页面已经就绪。"""

    if event.params.get("type") != "BackForwardCacheRestore":
        return False
    frame = event.params.get("frame")
    return isinstance(frame, dict) and not frame.get("parentId")


@asynccontextmanager
async def expect_navigation_settled(
    session: CdpTargetSession,
    *,
    timeout_seconds: float,
) -> AsyncIterator[Callable[[], Awaitable[str]]]:
    """在发出导航命令前登记等待，退出上下文前调用返回的函数即可等待页面就绪。

    订阅必须早于命令发出，否则事件会落在注册之前而永远等不到。
    """

    connection = session.connection
    async with (
        connection.expect_event(
            "Page.loadEventFired",
            session_id=session.session_id,
        ) as loaded,
        connection.expect_event(
            "Page.frameNavigated",
            session_id=session.session_id,
            predicate=_is_back_forward_cache_restore,
        ) as restored,
    ):

        async def settle() -> str:
            done, _pending = await asyncio.wait(
                (loaded, restored),
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise RuntimeError("等待页面加载完成超时")
            return LOAD_EVENT if loaded in done else BACK_FORWARD_CACHE_RESTORE

        yield settle

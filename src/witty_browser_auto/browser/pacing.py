"""按主机的最小请求间隔阀门。

抓取礼貌的实质是"同一站点两次请求之间留出间隔"。间隔按主机而不是全局计，否则访问 A 站
会拖慢访问 B 站；每个主机各有一把锁，并发调用同一主机时排队而不是同时冲过去。

时间基准取 `monotonic`：系统时钟被调整时不会出现负的等待或长时间挂起。
"""

from __future__ import annotations

import asyncio
from time import monotonic
from urllib.parse import urlsplit

MAX_INTERVAL_MS = 60_000

__all__ = ["MAX_INTERVAL_MS", "HostPacer", "host_of"]


def host_of(url: str) -> str:
    parts = urlsplit(url)
    return parts.netloc.casefold()


class HostPacer:
    """同一主机的连续请求之间强制留出最小间隔。"""

    def __init__(self, default_interval_ms: float = 0.0) -> None:
        self.default_interval_ms = self._validated(default_interval_ms)
        self._intervals: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _validated(interval_ms: float) -> float:
        if interval_ms < 0 or interval_ms > MAX_INTERVAL_MS:
            raise ValueError(f"请求间隔必须在 0 到 {MAX_INTERVAL_MS} 毫秒之间")
        return float(interval_ms)

    def configure(self, host: str, interval_ms: float) -> None:
        """为某个主机设定间隔；站点自己声明的 Crawl-delay 从这里生效。"""

        self._intervals[host.casefold()] = self._validated(interval_ms)

    def interval_for(self, host: str) -> float:
        # 站点声明与调用方配置取较大者：礼貌上限不能被调用方调低。
        return max(self._intervals.get(host.casefold(), 0.0), self.default_interval_ms)

    async def acquire(self, url: str) -> float:
        """必要时等待，返回实际等待的毫秒数。"""

        host = host_of(url)
        interval = self.interval_for(host)
        if interval <= 0:
            return 0.0
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            previous = self._last.get(host)
            now = monotonic()
            waited = 0.0
            if previous is not None:
                remaining = interval / 1000 - (now - previous)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    waited = remaining * 1000
            self._last[host] = monotonic()
            return round(waited, 3)

    def snapshot(self) -> dict[str, float]:
        """当前生效的各主机间隔，便于调用方核对节奏。"""

        return {host: self.interval_for(host) for host in sorted(self._intervals)}

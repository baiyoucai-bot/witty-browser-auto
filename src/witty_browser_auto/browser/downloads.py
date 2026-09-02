"""浏览器下载跟踪：用 Browser.setDownloadBehavior 接管落盘并等待完成。"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.cdp.transport import CdpConnection

_MAX_DOWNLOADS = 100
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


@dataclass(slots=True)
class DownloadRecord:
    guid: str
    url: str
    suggested_filename: str
    state: str = "pending"
    received_bytes: int = 0
    total_bytes: int = 0
    file_path: str | None = None
    saved_path: str | None = None
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "guid": self.guid,
            "url": self.url,
            "suggested_filename": self.suggested_filename,
            "state": self.state,
            "received_bytes": self.received_bytes,
            "total_bytes": self.total_bytes,
            "path": self.saved_path or self.file_path,
            "raw_path": self.file_path,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class DownloadTracker:
    """在浏览器连接级监听下载事件，把文件落到任务产物目录。

    使用 `allowAndName`：Chrome 以 GUID 命名落盘，避免同名覆盖；我们再按
    `suggestedFilename` 复制一份可读文件名，并保留 GUID 路径作溯源。
    `filePath` 只出现在 `Browser.downloadProgress` 的 completed 事件里，
    Page 域同名事件没有这个字段，所以只订 Browser 事件。
    """

    def __init__(self, connection: CdpConnection, download_root: Path) -> None:
        self._connection = connection
        self._download_root = download_root
        self._records: dict[str, DownloadRecord] = {}
        self._order: list[str] = []
        self._waiters: list[tuple[asyncio.Future[DownloadRecord], dict[str, Any]]] = []
        self._unsubscribers: list[Any] = []
        self._started = False

    @property
    def download_root(self) -> Path:
        return self._download_root

    async def start(self) -> None:
        if self._started:
            return
        self._download_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        await self._connection.call(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "downloadPath": str(self._download_root),
                "eventsEnabled": True,
            },
        )
        self._unsubscribers = [
            self._connection.subscribe("Browser.downloadWillBegin", self._on_will_begin),
            self._connection.subscribe("Browser.downloadProgress", self._on_progress),
        ]
        self._started = True

    def close(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        for future, _ in self._waiters:
            if not future.done():
                future.cancel()
        self._waiters.clear()
        self._started = False

    def list_downloads(self, *, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), _MAX_DOWNLOADS))
        records = [self._records[guid] for guid in self._order if guid in self._records]
        return [record.as_dict() for record in records[-limit:]]

    async def wait_for_download(
        self,
        *,
        suggested_filename: str | None = None,
        url_contains: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """等待匹配的下载完成；先看已有记录，没有再挂等待。"""

        if not self._started:
            raise RuntimeError("下载跟踪尚未启动")
        if not suggested_filename and not url_contains:
            raise ValueError("wait_for_download 至少提供 suggested_filename 或 url_contains")
        criteria = {
            "suggested_filename": suggested_filename,
            "url_contains": url_contains,
        }
        existing = self._find_completed(criteria)
        if existing is not None:
            return existing.as_dict()

        loop = asyncio.get_running_loop()
        future: asyncio.Future[DownloadRecord] = loop.create_future()
        self._waiters.append((future, criteria))
        try:
            record = await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError as exc:
            raise TimeoutError("等待下载超时") from exc
        finally:
            self._waiters = [
                item for item in self._waiters if item[0] is not future or not future.done()
            ]
        return record.as_dict()

    def _on_will_begin(self, event: CdpEvent) -> None:
        guid = event.params.get("guid")
        if not isinstance(guid, str) or not guid:
            return
        record = DownloadRecord(
            guid=guid,
            url=str(event.params.get("url", "")),
            suggested_filename=str(event.params.get("suggestedFilename", "") or guid),
        )
        self._records[guid] = record
        self._order.append(guid)
        self._trim()

    def _on_progress(self, event: CdpEvent) -> None:
        guid = event.params.get("guid")
        if not isinstance(guid, str):
            return
        record = self._records.get(guid)
        if record is None:
            record = DownloadRecord(
                guid=guid,
                url="",
                suggested_filename=guid,
            )
            self._records[guid] = record
            self._order.append(guid)
        record.received_bytes = int(event.params.get("receivedBytes") or 0)
        record.total_bytes = int(event.params.get("totalBytes") or 0)
        state = event.params.get("state")
        if isinstance(state, str):
            record.state = state
        if state != "completed":
            if state == "canceled":
                self._notify_waiters(record)
            return
        raw_path = event.params.get("filePath")
        if isinstance(raw_path, str) and raw_path:
            record.file_path = raw_path
            try:
                os.chmod(raw_path, 0o600)
            except OSError:
                pass
            record.saved_path = self._publish_readable_copy(record, Path(raw_path))
        record.completed_at = time.time()
        self._notify_waiters(record)

    def _publish_readable_copy(self, record: DownloadRecord, raw_path: Path) -> str:
        """把 GUID 文件复制成可读文件名，避免同名覆盖。"""

        if not raw_path.is_file():
            return str(raw_path)
        preferred = _safe_filename(record.suggested_filename) or raw_path.name
        target = self._unique_path(preferred)
        try:
            target.write_bytes(raw_path.read_bytes())
            os.chmod(target, 0o600)
            return str(target)
        except OSError:
            return str(raw_path)

    def _unique_path(self, filename: str) -> Path:
        candidate = self._download_root / filename
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        for index in range(1, 1000):
            alt = self._download_root / f"{stem}-{index}{suffix}"
            if not alt.exists():
                return alt
        return self._download_root / f"{stem}-{time.time_ns()}{suffix}"

    def _find_completed(self, criteria: dict[str, Any]) -> DownloadRecord | None:
        for guid in reversed(self._order):
            record = self._records.get(guid)
            if record is None or record.state != "completed":
                continue
            if _matches(record, criteria):
                return record
        return None

    def _notify_waiters(self, record: DownloadRecord) -> None:
        remaining: list[tuple[asyncio.Future[DownloadRecord], dict[str, Any]]] = []
        for future, criteria in self._waiters:
            if future.done():
                continue
            if record.state == "completed" and _matches(record, criteria):
                future.set_result(record)
            else:
                remaining.append((future, criteria))
        self._waiters = remaining

    def _trim(self) -> None:
        while len(self._order) > _MAX_DOWNLOADS:
            old = self._order.pop(0)
            self._records.pop(old, None)


def _matches(record: DownloadRecord, criteria: dict[str, Any]) -> bool:
    suggested = criteria.get("suggested_filename")
    if isinstance(suggested, str) and suggested:
        if record.suggested_filename != suggested and not record.suggested_filename.endswith(
            suggested
        ):
            # 也允许只匹配 basename。
            if Path(record.suggested_filename).name != suggested:
                return False
    url_contains = criteria.get("url_contains")
    if isinstance(url_contains, str) and url_contains:
        if url_contains not in record.url:
            return False
    return True


def _safe_filename(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", name).strip("._")
    return cleaned[:180]

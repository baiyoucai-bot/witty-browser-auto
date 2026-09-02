"""文件上传路径校验、下载跟踪与工具门面。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from witty_browser_auto.agent.file_tools import (
    build_upload_command,
    resolve_upload_path_arguments,
)
from witty_browser_auto.browser.downloads import DownloadTracker
from witty_browser_auto.browser.files import resolve_upload_paths
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.domain.errors import PolicyViolationError
from witty_browser_auto.domain.models import (
    ActionKind,
    DriverCapabilities,
    ExecutionScope,
    ModelToolCall,
    Observation,
    TaskSpec,
)
from witty_browser_auto.domain.protocols import DownloadInspectionProvider
from witty_browser_auto.toolkit import BrowserToolkit


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._handlers: dict[str, list] = {}

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, params or {}))
        return {}

    def subscribe(self, method: str, handler, *, session_id=None):
        self._handlers.setdefault(method, []).append(handler)

        def unsubscribe() -> None:
            self._handlers[method].remove(handler)

        return unsubscribe

    def emit(self, method: str, params: dict[str, Any]) -> None:
        event = CdpEvent(method=method, params=params, session_id=None)
        for handler in list(self._handlers.get(method, ())):
            handler(event)


class _FileDriver:
    def __init__(self, tmp_path: Path) -> None:
        self.artifact_root = tmp_path
        self.commands: list[Any] = []
        self.downloads = [
            {
                "guid": "g1",
                "url": "https://example.com/report.csv?token=secret",
                "suggested_filename": "report.csv",
                "state": "completed",
                "path": str(tmp_path / "report.csv"),
                "received_bytes": 12,
                "total_bytes": 12,
            }
        ]

    @property
    def capabilities(self) -> DriverCapabilities:
        return DriverCapabilities(dom=True, files=True)

    async def start(self) -> None:
        return None

    async def open(self, url: str) -> str:
        return "surface"

    async def observe(self, *, force: bool = False) -> Observation:
        return Observation(
            surface_id="surface",
            url="https://example.com/upload",
            title="upload",
            version=1,
            fingerprint="fp-1",
            summary="upload page",
            candidates=(),
        )

    async def execute(self, command):
        from witty_browser_auto.domain.models import ActionReceipt

        self.commands.append(command)
        return ActionReceipt(
            action_id=command.action_id,
            success=True,
            outcome_known=True,
            message="ok",
            duration_ms=1.0,
            data={"files": [{"name": "a.txt", "size": 3}], "file_count": 1},
        )

    async def verify(self, condition):
        from witty_browser_auto.domain.models import VerificationResult

        return VerificationResult(True, "ok")

    async def capture_evidence(self, label: str) -> Path:
        path = self.artifact_root / f"{label}.png"
        path.write_bytes(b"png")
        return path

    async def close(self) -> None:
        return None

    async def list_downloads(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return self.downloads[:limit]

    async def wait_for_download(
        self,
        *,
        suggested_filename: str | None = None,
        url_contains: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        return self.downloads[0]


def _task(tmp_path: Path, **inputs: str) -> TaskSpec:
    return TaskSpec(
        "task",
        "上传发票",
        "https://example.com/upload",
        ExecutionScope("project"),
        inputs=dict(inputs),
    )


def test_resolve_upload_paths_rejects_relative_missing_and_directories(tmp_path: Path) -> None:
    file_path = tmp_path / "invoice.txt"
    file_path.write_text("body")

    assert resolve_upload_paths([str(file_path)]) == [file_path.resolve()]

    with pytest.raises(PolicyViolationError, match="绝对路径"):
        resolve_upload_paths(["invoice.txt"])
    with pytest.raises(PolicyViolationError, match="不存在"):
        resolve_upload_paths([str(tmp_path / "missing.bin")])
    with pytest.raises(PolicyViolationError, match="普通文件"):
        resolve_upload_paths([str(tmp_path)])


def test_path_input_keys_are_resolved_from_task_inputs(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("aaa")
    paths = resolve_upload_path_arguments(
        {"path_input_keys": ["invoice_path"]},
        {"invoice_path": str(file_path)},
    )
    assert paths == [str(file_path.resolve())]


def test_download_tracker_renames_guid_file_and_wakes_waiters(tmp_path: Path) -> None:
    connection = _FakeConnection()
    tracker = DownloadTracker(connection, tmp_path / "downloads")

    async def scenario() -> None:
        await tracker.start()
        assert connection.calls[0][0] == "Browser.setDownloadBehavior"
        assert connection.calls[0][1]["behavior"] == "allowAndName"

        waiting = asyncio.create_task(
            tracker.wait_for_download(suggested_filename="report.csv", timeout_seconds=2)
        )
        await asyncio.sleep(0)
        connection.emit(
            "Browser.downloadWillBegin",
            {
                "guid": "guid-1",
                "url": "https://example.com/report.csv",
                "suggestedFilename": "report.csv",
            },
        )
        raw = tmp_path / "downloads" / "guid-1"
        raw.write_bytes(b"name,amount\n")
        connection.emit(
            "Browser.downloadProgress",
            {
                "guid": "guid-1",
                "totalBytes": 12,
                "receivedBytes": 12,
                "state": "completed",
                "filePath": str(raw),
            },
        )
        record = await waiting
        saved = Path(record["path"])
        assert record["suggested_filename"] == "report.csv"
        assert saved.name == "report.csv"
        listed = tracker.list_downloads()
        assert listed[0]["state"] == "completed"
        tracker.close()
        return saved

    saved_path = asyncio.run(scenario())
    assert saved_path.read_bytes() == b"name,amount\n"
    assert oct(saved_path.stat().st_mode & 0o777) == "0o600"


def test_upload_files_and_list_downloads_reach_the_driver(tmp_path: Path) -> None:
    file_path = tmp_path / "invoice.txt"
    file_path.write_text("invoice")
    driver = _FileDriver(tmp_path)
    assert isinstance(driver, DownloadInspectionProvider)
    toolkit = BrowserToolkit(driver, _task(tmp_path, invoice_path=str(file_path)))

    async def scenario() -> None:
        uploaded = await toolkit.upload_files(
            locator={"strategy": "css", "value": "#file"},
            path_input_keys=["invoice_path"],
        )
        assert uploaded.success
        assert driver.commands[0].kind is ActionKind.UPLOAD_FILES
        assert driver.commands[0].file_paths == (str(file_path.resolve()),)

        listed = await toolkit.list_downloads()
        assert listed.success
        # 下载 URL 里的敏感查询参数必须被脱敏。
        assert "secret" not in listed.data["downloads"][0]["url"]

        waited = await toolkit.wait_for_download(suggested_filename="report.csv")
        assert waited.success
        assert waited.data["suggested_filename"] == "report.csv"

    asyncio.run(scenario())


def test_build_upload_command_requires_exactly_one_target(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("a")
    task = _task(tmp_path, invoice_path=str(file_path))
    with pytest.raises(ValueError, match="target_id 或 locator"):
        build_upload_command(
            ModelToolCall(
                "c1",
                "upload_files",
                {"paths": [str(file_path)]},
            ),
            task=task,
            action_id="a1",
        )

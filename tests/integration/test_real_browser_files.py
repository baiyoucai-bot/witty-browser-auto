"""用真实 Chrome 验证文件上传与下载接管。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from aiohttp import web

from witty_browser_auto.config import AppConfig, BrowserConfig, StorageConfig
from witty_browser_auto.toolkit import launch_browser_toolkit

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("WITTY_BROWSER_AUTO_RUN_BROWSER_TESTS") != "1",
        reason="设置 WITTY_BROWSER_AUTO_RUN_BROWSER_TESTS=1 后执行真实浏览器测试",
    ),
]

_HOME = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>文件验收</title></head>
<body>
  <input id="file" type="file" />
  <input id="multi" type="file" multiple />
  <a id="dl" href="/report.csv" download="report.csv">下载报表</a>
  <pre id="state">idle</pre>
<script>
const dump = (el) => [...el.files].map(f => f.name + ':' + f.size).join('|');
document.getElementById('file').addEventListener('change', () => {
  document.getElementById('state').textContent = 'file=' + dump(file);
});
document.getElementById('multi').addEventListener('change', () => {
  document.getElementById('state').textContent = 'multi=' + dump(multi);
});
document.getElementById('dl').addEventListener('click', () => {
  document.getElementById('state').textContent = 'downloading';
});
</script>
</body></html>"""

_CSV = "name,amount\nalice,12\nbob,34\n"


async def _serve() -> tuple[web.AppRunner, str]:
    app = web.Application()

    async def home(_: web.Request) -> web.Response:
        return web.Response(text=_HOME, content_type="text/html")

    async def report(_: web.Request) -> web.Response:
        return web.Response(
            text=_CSV,
            content_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="report.csv"'},
        )

    app.router.add_get("/", home)
    app.router.add_get("/report.csv", report)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("文件验收服务未返回监听端口")
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}/"


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        browser=BrowserConfig(
            headless=True,
            profile_root=tmp_path / "profiles",
            command_timeout_seconds=10,
            launch_timeout_seconds=20,
        ),
        storage=StorageConfig(
            memory_database=tmp_path / "memory.db",
            artifact_root=tmp_path / "artifacts",
        ),
    )


def test_real_chrome_uploads_files_and_reads_them_back(tmp_path: Path) -> None:
    invoice = tmp_path / "invoice.txt"
    invoice.write_text("invoice-body", encoding="utf-8")
    extra = tmp_path / "extra.csv"
    extra.write_text("a,b\n1,2\n", encoding="utf-8")

    async def scenario() -> None:
        runner, url = await _serve()
        try:
            async with launch_browser_toolkit(
                url,
                config=_config(tmp_path),
                task_id="file-upload-e2e",
                inputs={"invoice_path": str(invoice)},
            ) as toolkit:
                await toolkit.observe()

                uploaded = await toolkit.upload_files(
                    locator={"strategy": "css", "value": "#file"},
                    path_input_keys=["invoice_path"],
                    expect_kind="text_contains",
                    expect_value="invoice.txt",
                )
                assert uploaded.success, uploaded.message
                assert uploaded.data["file_count"] == 1
                assert uploaded.data["files"][0]["name"] == "invoice.txt"
                assert uploaded.idempotent is False

                multi = await toolkit.upload_files(
                    locator={"strategy": "css", "value": "#multi"},
                    paths=[str(invoice), str(extra)],
                    expect_kind="text_contains",
                    expect_value="extra.csv",
                )
                assert multi.success, multi.message
                assert multi.data["file_count"] == 2

                # 相对路径与目录必须在触碰浏览器前被拒绝。
                blocked = await toolkit.upload_files(
                    locator={"strategy": "css", "value": "#file"},
                    paths=["invoice.txt"],
                )
                assert blocked.success is False
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_captures_downloads_with_readable_names(tmp_path: Path) -> None:
    download_path: Path | None = None

    async def scenario() -> None:
        nonlocal download_path
        runner, url = await _serve()
        try:
            async with launch_browser_toolkit(
                url, config=_config(tmp_path), task_id="file-download-e2e"
            ) as toolkit:
                await toolkit.observe()
                # 先挂等待再触发，避免下载完成事件在订阅前溜走。
                waiting = asyncio.create_task(
                    toolkit.wait_for_download(suggested_filename="report.csv", timeout_seconds=10)
                )
                clicked = await toolkit.click_locator(
                    {"strategy": "css", "value": "#dl"},
                    expect_kind="text_contains",
                    expect_value="downloading",
                )
                assert clicked.success, clicked.message
                waited = await waiting
                assert waited.success, waited.message
                download_path = Path(waited.data["path"])
                assert waited.data["suggested_filename"] == "report.csv"

                listed = await toolkit.list_downloads()
                assert listed.success
                assert any(
                    item["suggested_filename"] == "report.csv" for item in listed.data["downloads"]
                )
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

    assert download_path is not None
    assert download_path.name == "report.csv"
    assert download_path.read_text(encoding="utf-8") == _CSV
    assert oct(download_path.stat().st_mode & 0o777) == "0o600"

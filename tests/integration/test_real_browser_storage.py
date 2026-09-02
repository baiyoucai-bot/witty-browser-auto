"""用真实 Chrome 验证 Cookie 与 Web Storage 在 headless/后台下可读写。"""

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

_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>存储验收</title></head>
<body>
<script>
Object.defineProperty(document, 'visibilityState', {
  configurable: true,
  get: () => 'hidden',
});
localStorage.setItem('theme', 'dark');
sessionStorage.setItem('step', '1');
document.cookie = 'sid=abc; path=/';
</script>
<pre id="vis"></pre>
<script>document.getElementById('vis').textContent = document.visibilityState;</script>
</body></html>"""


async def _serve() -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text=_PAGE, content_type="text/html"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("存储验收服务未返回监听端口")
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


def test_real_chrome_reads_and_writes_storage_while_page_hidden(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _serve()
        try:
            async with launch_browser_toolkit(
                url,
                config=_config(tmp_path),
                task_id="storage-e2e",
                inputs={"session_token": "injected-sid"},
            ) as toolkit:
                await toolkit.observe()

                cookies = await toolkit.read_cookies()
                assert cookies.success, cookies.message
                names = {item["name"] for item in cookies.data["cookies"]}
                assert "sid" in names

                wrote = await toolkit.set_cookie(
                    name="extra",
                    value_input_key="session_token",
                )
                assert wrote.success, wrote.message
                after = await toolkit.read_cookies(names=["extra"])
                assert after.success
                assert after.data["cookies"][0]["value"] == "injected-sid"

                keys = await toolkit.read_web_storage("local")
                assert keys.success
                assert "theme" in keys.data["keys"]

                value = await toolkit.read_web_storage("local", key="theme")
                assert value.success
                assert value.data["value"] == "dark"

                updated = await toolkit.write_web_storage(
                    "local",
                    "theme",
                    value="light",
                )
                assert updated.success
                reread = await toolkit.read_web_storage("local", key="theme")
                assert reread.success
                assert reread.data["value"] == "light"

                removed = await toolkit.write_web_storage(
                    "session",
                    "step",
                    remove=True,
                )
                assert removed.success
                session_keys = await toolkit.read_web_storage("session")
                assert session_keys.success
                assert "step" not in session_keys.data["keys"]
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

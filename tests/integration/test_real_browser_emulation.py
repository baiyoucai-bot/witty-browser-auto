"""用真实 Chrome 验证环境模拟真的改变了页面看到的世界。

判据取"页面自己报出来的值"而不是"我们下发了什么参数"：服务端按 UA 返回不同页面、
页面自己读 innerWidth 与媒体查询，这样才能证明模拟是生效的而不只是命令发出去了。
"""

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

# 带 viewport meta，页面才会按请求宽度布局而不是退回 980。
_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title></head>
<body>
<h1 id="variant">{variant}</h1>
<div id="metrics"></div>
<script>
document.getElementById('metrics').textContent = [
  'w=' + window.innerWidth,
  'dpr=' + window.devicePixelRatio,
  'touch=' + navigator.maxTouchPoints,
  'dark=' + matchMedia('(prefers-color-scheme: dark)').matches,
  'tz=' + Intl.DateTimeFormat().resolvedOptions().timeZone,
].join(' ');
</script>
</body></html>"""


async def _serve(seen: list[str]) -> tuple[web.AppRunner, str]:
    async def page(request: web.Request) -> web.Response:
        agent = request.headers.get("User-Agent", "")
        seen.append(agent)
        # 真实站点就是这样按 UA 分流的，桌面版与移动版是两份完全不同的 DOM。
        mobile = "iPhone" in agent or "Android" in agent
        variant = "移动版首页" if mobile else "桌面版首页"
        return web.Response(
            text=_PAGE.format(title=variant, variant=variant), content_type="text/html"
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("模拟验收服务未返回监听端口")
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}/"


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        browser=BrowserConfig(
            headless=True,
            profile_root=tmp_path / "profiles",
            command_timeout_seconds=15,
            launch_timeout_seconds=20,
        ),
        storage=StorageConfig(
            memory_database=tmp_path / "m.db",
            artifact_root=tmp_path / "artifacts",
        ),
    )


def test_emulation_changes_what_the_page_sees(tmp_path: Path) -> None:
    seen: list[str] = []

    async def scenario() -> None:
        runner, base_url = await _serve(seen)
        try:
            async with launch_browser_toolkit(
                base_url,
                goal="移动端验收",
                config=_config(tmp_path),
                allowed_origins=[base_url],
            ) as toolkit:

                async def summary() -> str:
                    return (await toolkit.observe(force=True)).summary

                assert "桌面版首页" in await summary()

                applied = await toolkit.emulate_environment(device="iphone_15")
                assert applied.success, applied.message
                effective = applied.data["effective"]
                assert effective["userAgentDataMobile"] is True, "客户端提示必须一起改"
                assert effective["maxTouchPoints"] == 5, "mobile 标志不会自动带来触控"

                # 服务端按 UA 分流，重新导航才会拿到移动版 DOM。
                await toolkit.navigate(base_url)
                assert "移动版首页" in await summary()
                assert any("iPhone" in agent for agent in seen)

                page_text = await summary()
                assert "w=393" in page_text, f"页面未按请求宽度布局：{page_text}"
                assert "dpr=3" in page_text
                assert "touch=5" in page_text

                # 各维度相互独立，只改深色与时区不应丢掉设备模拟。
                await toolkit.emulate_environment(color_scheme="dark", timezone="Asia/Tokyo")
                await toolkit.navigate(base_url)
                page_text = await summary()
                assert "dark=true" in page_text
                assert "tz=Asia/Tokyo" in page_text
                assert "w=393" in page_text, "改配色不应丢掉设备视口"

                # 新标签页不继承任何覆盖，切页后必须整套重施。
                opened = await toolkit.open_tab(base_url)
                assert opened.success, opened.message
                page_text = await summary()
                assert "w=393" in page_text, f"新标签页丢失了设备模拟：{page_text}"
                assert "dark=true" in page_text, f"新标签页丢失了配色模拟：{page_text}"

                cleared = await toolkit.emulate_environment(reset=True)
                assert cleared.success
                await toolkit.navigate(base_url)
                page_text = await summary()
                assert "桌面版首页" in page_text
                assert "dark=false" in page_text
                assert "touch=0" in page_text
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_offline_emulation_blocks_page_requests(tmp_path: Path) -> None:
    seen: list[str] = []

    async def scenario() -> None:
        runner, base_url = await _serve(seen)
        try:
            async with launch_browser_toolkit(
                base_url,
                goal="离线验收",
                config=_config(tmp_path),
                allowed_origins=[base_url],
            ) as toolkit:
                await toolkit.emulate_environment(network_preset="offline")
                before = len(seen)
                result = await toolkit.navigate(base_url)
                # 离线下导航必须失败，且请求根本到不了服务端。
                assert result.success is False, "离线状态下导航不应成功"
                assert len(seen) == before, "离线状态下请求仍然打到了服务端"

                await toolkit.emulate_environment(network_preset="no_throttle")
                recovered = await toolkit.navigate(base_url)
                assert recovered.success, recovered.message
                assert len(seen) > before
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

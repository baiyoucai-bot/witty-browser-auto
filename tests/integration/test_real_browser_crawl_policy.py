"""用真实 Chrome 验证 robots.txt 读取与遵守设置下的导航闸门。

`fetch_robots_txt` 在页面上下文里发 `fetch`，假驱动证明不了它真的取到了文件；而"被禁止的
地址是否真的没有被访问"只有让服务端记录收到过哪些请求才能证伪——工具没报错不等于没访问。
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

_ROBOTS = """User-agent: *
Disallow: /admin/
Crawl-delay: 1

User-agent: WittyBrowserAuto
Disallow: /secret/
Allow: /secret/open.html
Sitemap: http://127.0.0.1/sitemap.xml
"""

_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title></head><body><main><h1>{title}</h1><p>正文内容</p></main>
</body></html>"""


async def _start_server() -> tuple[web.AppRunner, str, list[str]]:
    """返回 runner、基地址，以及服务端实际收到的路径列表。"""

    seen: list[str] = []

    async def robots(request: web.Request) -> web.Response:
        seen.append(request.path)
        return web.Response(text=_ROBOTS, content_type="text/plain")

    async def page(request: web.Request) -> web.Response:
        seen.append(request.path)
        return web.Response(text=_PAGE.format(title=request.path), content_type="text/html")

    app = web.Application()
    app.router.add_get("/robots.txt", robots)
    app.router.add_get("/{tail:.*}", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("抓取策略验收服务未返回监听端口")
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}", seen


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


def test_real_chrome_reads_robots_and_reports_verdict(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, base, seen = await _start_server()
        try:
            async with launch_browser_toolkit(f"{base}/", config=_config(tmp_path)) as toolkit:
                policy = await toolkit.check_crawl_policy(url=f"{base}/secret/x")
                assert policy.success, policy.message
                # robots.txt 真的被取到了：服务端记录到这次请求。
                assert "/robots.txt" in seen
                assert policy.data["allowed"] is False
                assert policy.data["matched_rule"]["pattern"] == "/secret/"
                assert policy.data["policy"]["matched_agent"] == "wittybrowserauto"
                assert policy.data["policy"]["sitemaps"] == ["http://127.0.0.1/sitemap.xml"]

                # 更长的 Allow 规则胜出。
                opened = await toolkit.check_crawl_policy(url=f"{base}/secret/open.html")
                assert opened.data["allowed"] is True
                assert opened.data["cached"] is True

                # 通用分组的规则与 Crawl-delay 只在匹配 * 时生效。
                other = await toolkit.check_crawl_policy(
                    url=f"{base}/admin/panel", agent="SomeOtherBot"
                )
                assert other.data["allowed"] is False
                assert other.data["policy"]["crawl_delay_seconds"] == 1.0
                # 默认会话是纯咨询模式：如实报出站点声明的间隔，但不擅自替调用方限速。
                assert other.data["pacing_interval_ms"] == 0.0
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_respect_robots_blocks_the_request_entirely(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, base, seen = await _start_server()
        try:
            async with launch_browser_toolkit(
                f"{base}/",
                config=_config(tmp_path),
                respect_robots=True,
            ) as toolkit:
                blocked = await toolkit.navigate(f"{base}/secret/x")
                assert blocked.success is False
                assert "robots.txt 禁止抓取" in blocked.message
                # 判据是服务端从没收到过这个路径，而不是"工具报了失败"。
                assert "/secret/x" not in seen

                allowed = await toolkit.navigate(f"{base}/secret/open.html")
                assert allowed.success, allowed.message
                assert "/secret/open.html" in seen
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_declared_crawl_delay_paces_navigation(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, base, seen = await _start_server()
        try:
            # 用匹配 * 分组的 agent，站点声明的 1 秒 Crawl-delay 才会落到节奏阀门上。
            async with launch_browser_toolkit(
                f"{base}/",
                config=_config(tmp_path),
                respect_robots=True,
                crawl_agent="SomeOtherBot",
            ) as toolkit:
                policy = await toolkit.check_crawl_policy(url=f"{base}/a")
                assert policy.data["pacing_interval_ms"] == 1000.0

                loop = asyncio.get_running_loop()
                await toolkit.navigate(f"{base}/a")
                started = loop.time()
                await toolkit.navigate(f"{base}/b")
                elapsed_ms = (loop.time() - started) * 1000

                # 第二次导航必须被压到 1 秒之后；留出调度与页面加载的余量。
                assert elapsed_ms >= 800, elapsed_ms
                assert "/a" in seen and "/b" in seen
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_default_session_ignores_robots(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, base, seen = await _start_server()
        try:
            # 不打开遵守开关时，robots.txt 不影响交互场景的导航。
            async with launch_browser_toolkit(f"{base}/", config=_config(tmp_path)) as toolkit:
                result = await toolkit.navigate(f"{base}/secret/x")
                assert result.success, result.message
                assert "/secret/x" in seen
                assert "/robots.txt" not in seen
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

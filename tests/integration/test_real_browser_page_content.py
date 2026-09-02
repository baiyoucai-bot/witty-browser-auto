"""用真实 Chrome 验证页面 Markdown 提取与链接清单。

HTML→Markdown 转换是一段在页面里跑的固定脚本，假驱动证明不了它的正确性：标题层级、
嵌套列表、代码块、表格、行内链接是不是真的被保留，导航与页脚是不是真的被剥掉，只有让
真实浏览器渲染一遍才能证伪。
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

# 正文之外故意放满样板：导航、页眉、页脚、侧栏、脚本、隐藏块。
_ARTICLE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>接口指南</title>
<style>.hidden{display:none}</style></head>
<body>
  <header><h1>站点名称</h1></header>
  <nav><a href="/nav-a">导航甲</a><a href="/nav-b">导航乙</a></nav>
  <aside><h3>侧栏推荐</h3><a href="/side">侧栏链接</a></aside>
  <main>
    <h1>接口指南</h1>
    <p>这是<strong>正文段落</strong>，包含<em>强调</em>与<code>inline_code</code>。</p>
    <h2>使用步骤</h2>
    <ol start="1">
      <li>申请密钥
        <ul><li>登录控制台</li><li>创建应用</li></ul>
      </li>
      <li>发起调用</li>
    </ol>
    <pre><code>curl https://api.example.com/v1/orders</code></pre>
    <blockquote>调用前请先确认配额。</blockquote>
    <table>
      <tr><th>参数</th><th>说明</th></tr>
      <tr><td>page</td><td>页码</td></tr>
    </table>
    <p>详情见<a href="/docs/detail">详情页</a>与<a href="https://other.example/out">外站</a>。</p>
    <img src="/logo.png" alt="站点标识">
    <h2>配额说明</h2>
__FILLER__
    <div class="hidden"><p>不可见的隐藏段落</p></div>
    <script>window.__junk = "脚本内容";</script>
  </main>
  <footer><p>页脚版权</p><a href="/legal">法律声明</a></footer>
</body></html>"""

# 截断要在"确实需要截断"的内容上验证，因此正文补足到明显超过 1000 字符预算。
_FILLER = "\n".join(
    f"    <p>第 {index} 段配额说明：每个应用默认每分钟六十次调用，超出后服务端返回 429 "
    f"并在响应头给出重置时间；需要更高配额时在控制台提交工单，说明业务场景、预计峰值"
    f"与调用时段分布，审核通过后新配额在次日零点生效。</p>"
    for index in range(1, 13)
)
_ARTICLE = _ARTICLE.replace("__FILLER__", _FILLER)


async def _start_server() -> tuple[web.AppRunner, str]:
    app = web.Application()

    async def article(request: web.Request) -> web.Response:
        return web.Response(text=_ARTICLE, content_type="text/html")

    app.router.add_get("/", article)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("页面内容验收服务未返回监听端口")
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


def test_real_chrome_markdown_keeps_structure_and_drops_boilerplate(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _start_server()
        try:
            async with launch_browser_toolkit(url, config=_config(tmp_path)) as toolkit:
                result = await toolkit.read_page_markdown()
                assert result.success, result.message
                markdown = result.data["markdown"]

                # 结构保真：标题层级、强调、行内代码、代码块、引用、表格。
                assert "# 接口指南" in markdown
                assert "## 使用步骤" in markdown
                assert "**正文段落**" in markdown
                assert "*强调*" in markdown
                assert "`inline_code`" in markdown
                assert "```" in markdown and "curl https://api.example.com/v1/orders" in markdown
                assert "> 调用前请先确认配额。" in markdown
                assert "| 参数 | 说明 |" in markdown
                assert "| page | 页码 |" in markdown

                # 嵌套列表：有序项与其下的无序子项都要在。
                assert "1. 申请密钥" in markdown
                assert "- 登录控制台" in markdown
                assert "2. 发起调用" in markdown

                # 行内链接换算成绝对地址。
                assert f"[详情页]({url}docs/detail)" in markdown
                assert "[外站](https://other.example/out)" in markdown

                # 样板必须被剥掉：导航、侧栏、页脚、脚本与隐藏块都不能出现。
                for junk in (
                    "导航甲",
                    "侧栏推荐",
                    "页脚版权",
                    "法律声明",
                    "脚本内容",
                    "不可见的隐藏段落",
                ):
                    assert junk not in markdown, junk

                # 默认不带图片引用。
                assert "![站点标识]" not in markdown
                with_images = await toolkit.read_page_markdown(include_images=True)
                assert f"![站点标识]({url}logo.png)" in with_images.data["markdown"]

                assert result.data["content_root"] == "main"
                assert result.data["truncated"] is False
                assert result.data["title"] == "接口指南"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_markdown_respects_selector_and_char_budget(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _start_server()
        try:
            async with launch_browser_toolkit(url, config=_config(tmp_path)) as toolkit:
                # 显式选择器优先于自动判定：只要侧栏那一块。
                scoped = await toolkit.read_page_markdown(selector="aside")
                assert "侧栏推荐" in scoped.data["markdown"]
                assert "接口指南" not in scoped.data["markdown"]

                # 关闭主内容剥离后，导航与页脚会一起进来。
                whole = await toolkit.read_page_markdown(only_main_content=False)
                assert "页脚版权" in whole.data["markdown"]

                clipped = await toolkit.read_page_markdown(max_chars=1000)
                assert clipped.data["truncated"] is True
                assert clipped.data["char_count"] == 1000
                # 截断只影响返回内容，真实总长仍然如实报告。
                assert clipped.data["total_char_count"] > 1000
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_lists_links_with_absolute_urls(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _start_server()
        try:
            async with launch_browser_toolkit(url, config=_config(tmp_path)) as toolkit:
                listed = await toolkit.list_page_links()
                assert listed.success, listed.message
                hrefs = [item["href"] for item in listed.data["links"]]
                # 链接清单覆盖整页，包括导航与页脚——它服务的是遍历而不是阅读。
                assert f"{url}nav-a" in hrefs
                assert f"{url}legal" in hrefs
                assert "https://other.example/out" in hrefs
                assert all(item["href"].startswith("http") for item in listed.data["links"])

                same_origin = await toolkit.list_page_links(same_origin_only=True)
                assert all(item["same_origin"] for item in same_origin.data["links"])
                assert "https://other.example/out" not in [
                    item["href"] for item in same_origin.data["links"]
                ]

                filtered = await toolkit.list_page_links(contains="/docs/")
                assert [item["href"] for item in filtered.data["links"]] == [f"{url}docs/detail"]

                with_images = await toolkit.list_page_links(include_images=True)
                assert with_images.data["images"][0]["alt"] == "站点标识"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

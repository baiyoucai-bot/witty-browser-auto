"""用真实 Chrome 验证外部调用方看到的工具门面行为与技能文档一致。

单元测试用假驱动证明命令构造正确，但外部智能体真正依赖的是"按文档写的代码能跑通"：
按键是否真的触发表单提交、历史导航后观察是否作废、密码值是否真的读不出来，这些只有
跑真实浏览器才能证伪。
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

_HOME = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>工具会话首页</title></head>
<body>
  <form action="/search" method="get">
    <label for="q">关键词</label>
    <input id="q" name="q" autocomplete="off">
    <label for="pwd">密码</label>
    <input id="pwd" name="pwd" type="password" value="local-secret">
    <button id="go" type="submit">搜索</button>
  </form>
  <p id="tip" data-testid="tip">首页提示</p>
  <button id="ghost" type="button" disabled hidden>隐藏按钮</button>
</body></html>"""


def _search_html(keyword: str) -> str:
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<title>结果页</title></head><body><p id="hit">命中：{keyword}</p>'
        "</body></html>"
    )


async def _start_toolkit_server() -> tuple[web.AppRunner, str]:
    app = web.Application()

    async def home(request: web.Request) -> web.Response:
        return web.Response(text=_HOME, content_type="text/html")

    async def search(request: web.Request) -> web.Response:
        return web.Response(
            text=_search_html(request.query.get("q", "")),
            content_type="text/html",
        )

    app.router.add_get("/", home)
    app.router.add_get("/search", search)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("工具会话验收服务未返回监听端口")
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}/"


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig(
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
    return config


def test_real_chrome_read_element_press_key_and_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _start_toolkit_server()
        try:
            async with launch_browser_toolkit(
                url,
                config=_config(tmp_path),
                inputs={"keyword": "测试关键词"},
                task_id="toolkit-e2e",
            ) as toolkit:
                observation = await toolkit.observe()
                textbox = next(
                    item
                    for item in observation.candidates
                    if item.role == "textbox" and "关键词" in item.name
                )

                read = await toolkit.read_element(textbox.target_id)
                assert read.success, read.message
                assert read.idempotent is True
                state = read.data
                assert state["tag"] == "input"
                assert state["role"] == "textbox"
                assert state["visible"] is True
                assert state["disabled"] is False
                assert state["box"]["width"] > 0
                assert state["attributes"]["id"] == "q"
                # 只读工具不消耗观察，后续元素调用仍可复用同一批候选。
                assert toolkit.observation is not None

                password = await toolkit.read_element(locator={"strategy": "css", "value": "#pwd"})
                assert password.success, password.message
                assert password.data["value"] is None
                assert password.data["value_masked"] is True
                assert password.data["value_length"] == len("local-secret")
                assert "local-secret" not in str(password.data)

                filled = await toolkit.input_text(textbox.target_id, input_key="keyword")
                assert filled.success, filled.message

                submitted = await toolkit.press_key(
                    "enter",
                    locator={"strategy": "css", "value": "#q"},
                    expect_kind="url_contains",
                    expect_value="/search",
                )
                assert submitted.success, submitted.message
                assert submitted.idempotent is False
                # 键名与修饰键之外的内容不应进入回执，避免按键成为凭据旁路。
                assert "测试关键词" not in str(submitted.data)

                after_submit = await toolkit.observe()
                assert "/search" in after_submit.url

                back = await toolkit.go_back()
                assert back.success, back.message
                home_again = await toolkit.observe()
                assert home_again.url.rstrip("/").endswith(url.rstrip("/").split("//")[-1])

                forward = await toolkit.go_forward()
                assert forward.success, forward.message
                assert "/search" in (await toolkit.observe()).url

                reloaded = await toolkit.reload(expect_kind="text_contains", expect_value="命中：")
                assert reloaded.success, reloaded.message
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_read_element_sees_hidden_and_disabled_targets(tmp_path: Path) -> None:
    """读取要能覆盖不可操作的元素，否则调用方无法解释动作为什么会失败。"""

    async def scenario() -> None:
        runner, url = await _start_toolkit_server()
        try:
            async with launch_browser_toolkit(
                url,
                config=_config(tmp_path),
                task_id="toolkit-e2e-hidden",
            ) as toolkit:
                await toolkit.observe()
                ghost = await toolkit.read_element(locator={"strategy": "css", "value": "#ghost"})
                assert ghost.success, ghost.message
                assert ghost.data["visible"] is False
                assert ghost.data["disabled"] is True
                assert ghost.data["attributes"]["id"] == "ghost"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

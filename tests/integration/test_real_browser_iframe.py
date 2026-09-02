"""用真实 Chrome 验证 iframe 定位与坐标换算。

iframe 的两条路径在协议层完全不同，且都无法用假驱动证伪：同站 iframe 的内容留在页面
Target 里但脚本作用域是独立的；跨站 iframe 会被 Chrome 放进独立渲染进程，页面会话既看
不到它的帧树也穿透不了它的 DOM，盒模型还是帧内局部坐标。

站点隔离按 site 而不是 origin 划分，所以"跨站"必须用不同主机名，只换端口不会触发 OOPIF。
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

_INNER = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>body{{margin:0}} #pad{{height:{pad}px}}</style></head>
<body>
  <div id="pad"></div>
  <label for="card">卡号</label>
  <input id="card" name="card" autocomplete="off">
  <button id="pay" type="button">确认支付</button>
  <p id="status">未支付</p>
  <script>
    document.querySelector('#pay').onclick = () => {{
      document.querySelector('#status').textContent =
        '已支付:' + document.querySelector('#card').value;
    }};
  </script>
</body></html>"""


def _outer_html(same_site_url: str, cross_site_url: str) -> str:
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        "<title>外层页面</title><style>body{margin:0} iframe{display:block;border:0}"
        "#same{position:absolute;left:40px;top:60px;width:320px;height:200px}"
        "#cross{position:absolute;left:420px;top:260px;width:320px;height:200px}"
        "</style></head><body>"
        '<p id="outer">外层文案</p>'
        f'<iframe id="same" title="同站框" src="{same_site_url}"></iframe>'
        f'<iframe id="cross" title="跨站框" src="{cross_site_url}"></iframe>'
        "</body></html>"
    )


async def _serve(routes: dict[str, str], host: str) -> tuple[web.AppRunner, int]:
    app = web.Application()
    for path, body in routes.items():

        async def handle(_request: web.Request, html: str = body) -> web.Response:
            return web.Response(text=html, content_type="text/html")

        app.router.add_get(path, handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("iframe 验收服务未返回监听端口")
    return runner, int(site._server.sockets[0].getsockname()[1])


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


def test_real_chrome_locates_and_operates_inside_same_site_and_cross_site_iframes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        # 0.0.0.0 让同一个服务同时可经 127.0.0.1 与 localhost 访问，从而构造真正的跨站帧。
        inner_runner, inner_port = await _serve(
            {
                "/same": _INNER.format(title="同站内页", pad=0),
                "/cross": _INNER.format(title="跨站内页", pad=0),
            },
            "0.0.0.0",
        )
        outer_runner, outer_port = await _serve(
            {
                "/": _outer_html(
                    f"http://127.0.0.1:{inner_port}/same",
                    f"http://localhost:{inner_port}/cross",
                )
            },
            "127.0.0.1",
        )
        try:
            async with launch_browser_toolkit(
                f"http://127.0.0.1:{outer_port}/",
                config=_config(tmp_path),
                inputs={"card_number": "6222-0000-1234"},
                task_id="iframe-e2e",
            ) as toolkit:
                await toolkit.observe()

                listed = await toolkit.list_frames()
                assert listed.success, listed.message
                assert listed.idempotent is True
                frames = listed.data["frames"]
                by_title = {frame["name"] or frame["url"]: frame for frame in frames}
                assert listed.data["frame_count"] == 3, by_title
                assert listed.data["child_frame_count"] == 2
                # 只换端口不算跨站，必须恰好有一个帧被 Chrome 判定为跨站独立进程。
                assert listed.data["cross_origin_frame_count"] == 1

                main = next(frame for frame in frames if frame["is_main"])
                same_site = next(
                    frame for frame in frames if not frame["is_main"] and not frame["cross_origin"]
                )
                cross_site = next(frame for frame in frames if frame["cross_origin"])
                assert same_site["parent_frame_id"] == main["frame_id"]
                assert cross_site["parent_frame_id"] == main["frame_id"]

                # 不带 frame_id 的定位器绝不能穿透到 iframe 里，否则作用域无法预测。
                leaked = await toolkit.read_element(
                    locator={"strategy": "css", "value": "#card", "timeout_seconds": 0.5}
                )
                assert leaked.success is False, leaked.data
                # 必须是"主框架里没有"，而不是"匹配到多个"——后者只是碰巧也失败。
                assert "尚未匹配元素" in leaked.message, leaked.message

                for frame in (same_site, cross_site):
                    frame_id = frame["frame_id"]
                    read = await toolkit.read_element(
                        locator={"strategy": "css", "value": "#card", "frame_id": frame_id}
                    )
                    assert read.success, f"{frame['url']}: {read.message}"
                    assert read.data["tag"] == "input"

                    typed = await toolkit.input_text_locator(
                        {"strategy": "css", "value": "#card", "frame_id": frame_id},
                        input_key="card_number",
                    )
                    assert typed.success, f"{frame['url']}: {typed.message}"

                    # 点击走视口坐标，跨站帧必须叠加宿主 iframe 偏移才会命中。
                    clicked = await toolkit.click_locator(
                        {
                            "strategy": "role",
                            "value": "button",
                            "name": "确认支付",
                            "frame_id": frame_id,
                        },
                        expect_kind="text_contains",
                        expect_value="已支付",
                    )
                    assert clicked.success, f"{frame['url']}: {clicked.message}"

                    status = await toolkit.read_element(
                        locator={"strategy": "css", "value": "#status", "frame_id": frame_id}
                    )
                    # 帧内读取同样要脱敏：任务输入不能借 iframe 绕过保护回到调用方。
                    assert status.data["text"].startswith("已支付:"), status.data
                    assert "6222-0000-1234" not in str(status.data)

                    # 帧内元素的包围盒必须与点击坐标同系，否则调用方无法与截图对齐。
                    button = await toolkit.read_element(
                        locator={"strategy": "css", "value": "#pay", "frame_id": frame_id}
                    )
                    expected_left = 40 if frame is same_site else 420
                    expected_top = 60 if frame is same_site else 260
                    assert button.data["box"]["x"] >= expected_left, button.data["box"]
                    assert button.data["box"]["y"] >= expected_top, button.data["box"]
        finally:
            await outer_runner.cleanup()
            await inner_runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_translates_coordinates_for_scrolled_cross_site_iframe(
    tmp_path: Path,
) -> None:
    """帧内滚动后盒模型仍是帧内局部坐标，偏移换算必须照样成立。"""

    async def scenario() -> None:
        inner_runner, inner_port = await _serve(
            {"/cross": _INNER.format(title="跨站长页", pad=600)},
            "0.0.0.0",
        )
        outer_runner, outer_port = await _serve(
            {
                "/": _outer_html(
                    "about:blank",
                    f"http://localhost:{inner_port}/cross",
                )
            },
            "127.0.0.1",
        )
        try:
            async with launch_browser_toolkit(
                f"http://127.0.0.1:{outer_port}/",
                config=_config(tmp_path),
                inputs={"card_number": "6222-9999-0000"},
                task_id="iframe-e2e-scroll",
            ) as toolkit:
                await toolkit.observe()
                frames = (await toolkit.list_frames()).data["frames"]
                cross_site = next(frame for frame in frames if frame["cross_origin"])
                frame_id = cross_site["frame_id"]

                # 目标在帧内 600px 之下，可操作性校验会先把它滚进帧视口再定位。
                clicked = await toolkit.click_locator(
                    {
                        "strategy": "role",
                        "value": "button",
                        "name": "确认支付",
                        "frame_id": frame_id,
                    },
                    expect_kind="text_contains",
                    expect_value="已支付",
                )
                assert clicked.success, clicked.message

                status = await toolkit.read_element(
                    locator={"strategy": "css", "value": "#status", "frame_id": frame_id}
                )
                assert status.data["text"].startswith("已支付"), status.data
        finally:
            await outer_runner.cleanup()
            await inner_runner.cleanup()

    asyncio.run(scenario())

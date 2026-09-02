"""用真实 Chrome 验证悬停、右键、双击、元素截图与新建标签页。

这批能力的正确性只有真实浏览器能证伪：右键是否真的触发 `contextmenu`、双击是否真的
产生 `dblclick`、`:hover` 是否真的生效、截图 clip 是否真的落在元素上，假驱动全都答不了。
"""

from __future__ import annotations

import asyncio
import os
import struct
import zlib
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
<html lang="zh-CN"><head><meta charset="utf-8"><title>指针验收</title>
<style>
  body { margin: 0; font: 16px sans-serif; }
  #nav { width: 200px; height: 50px; background: #ddd; margin: 20px; }
  #nav:hover { background: rgb(47, 111, 87); }
  #submenu, #ctxmenu, #editing { display: none; }
  #row { width: 240px; height: 40px; background: #eee; margin: 20px; }
  #spacer { height: 1600px; }
  #mark { width: 120px; height: 60px; background: rgb(255, 0, 255); margin-left: 30px; }
</style></head>
<body>
  <div id="nav">全部商品</div>
  <div id="submenu">全部分类</div>
  <div id="row">季度报表</div>
  <div id="ctxmenu">重命名</div>
  <div id="editing">编辑中</div>
  <p id="clicks">clicks=0</p>
  <a id="detail" href="/detail">详情</a>
  <div id="spacer"></div>
  <div id="mark">视口外色块</div>
<script>
const show = (id) => { document.getElementById(id).style.display = 'block'; };
let clicks = 0;
const nav = document.getElementById('nav');
nav.addEventListener('mouseover', () => show('submenu'));
const row = document.getElementById('row');
row.addEventListener('contextmenu', (e) => { e.preventDefault(); show('ctxmenu'); });
row.addEventListener('dblclick', () => show('editing'));
row.addEventListener('click', () => {
  clicks += 1;
  document.getElementById('clicks').textContent = 'clicks=' + clicks;
});
window.__navColor = () => getComputedStyle(nav).backgroundColor;
</script>
</body></html>"""

_DETAIL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>详情页</title></head>
<body><p id="detail-hit">详情内容</p></body></html>"""


def _png_pixel(data: bytes, x: int, y: int) -> tuple[int, int, int]:
    """最小 PNG 解码，只用于断言截图确实落在目标色块上。"""

    pos = 8
    width = height = color_type = 0
    idat = b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        if kind == b"IHDR":
            width, height, _bit, color_type = struct.unpack(">IIBB", data[pos + 8 : pos + 18])
        elif kind == b"IDAT":
            idat += data[pos + 8 : pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    channels = 4 if color_type == 6 else 3
    stride = width * channels
    prev = bytearray(stride)
    rows: list[bytes] = []
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset : offset + stride])
        offset += stride
        for index in range(stride):
            left = line[index - channels] if index >= channels else 0
            up = prev[index]
            up_left = prev[index - channels] if index >= channels else 0
            if filter_type == 1:
                line[index] = (line[index] + left) & 0xFF
            elif filter_type == 2:
                line[index] = (line[index] + up) & 0xFF
            elif filter_type == 3:
                line[index] = (line[index] + (left + up) // 2) & 0xFF
            elif filter_type == 4:
                delta = left + up - up_left
                pa, pb, pc = abs(delta - left), abs(delta - up), abs(delta - up_left)
                pred = left if (pa <= pb and pa <= pc) else (up if pb <= pc else up_left)
                line[index] = (line[index] + pred) & 0xFF
        rows.append(bytes(line))
        prev = line
    pixel = rows[y][x * channels : x * channels + 3]
    return (pixel[0], pixel[1], pixel[2])


async def _serve() -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text=_HOME, content_type="text/html"))
    app.router.add_get("/detail", lambda r: web.Response(text=_DETAIL, content_type="text/html"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("指针验收服务未返回监听端口")
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


def test_real_chrome_hover_right_click_and_double_click(tmp_path: Path) -> None:
    nav_shot_path: Path | None = None

    async def scenario() -> None:
        nonlocal nav_shot_path
        runner, url = await _serve()
        try:
            async with launch_browser_toolkit(
                url, config=_config(tmp_path), task_id="pointer-e2e"
            ) as toolkit:
                await toolkit.observe()

                hovered = await toolkit.hover(
                    locator={"strategy": "css", "value": "#nav"},
                    expect_kind="text_contains",
                    expect_value="全部分类",
                )
                assert hovered.success, hovered.message
                # 悬停不按下任何键，因此失败后可以安全重放。
                assert hovered.idempotent is True

                # CSS :hover 也必须真的生效，而不只是 JS 的 mouseover 被触发。
                nav_shot = await toolkit.capture_element_screenshot(
                    locator={"strategy": "css", "value": "#nav"}, label="悬停后导航"
                )
                nav_shot_path = Path(nav_shot.data["screenshot_path"])

                # 右键菜单的目标通常是行、卡片这类没有语义角色的元素，进不了候选列表。
                await toolkit.observe()
                right = await toolkit.right_click(
                    locator={"strategy": "css", "value": "#row"},
                    expect_kind="text_contains",
                    expect_value="重命名",
                )
                assert right.success, right.message
                assert right.idempotent is False

                await toolkit.observe()
                double = await toolkit.double_click(
                    locator={"strategy": "css", "value": "#row"},
                    expect_kind="text_contains",
                    expect_value="编辑中",
                )
                assert double.success, double.message

                # 真实双击必须同时产生两次 click，只发一轮 clickCount=2 会少一次。
                clicks = await toolkit.read_element(locator={"strategy": "css", "value": "#clicks"})
                assert clicks.data["text"] == "clicks=2"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

    # CSS :hover 也必须真的生效，而不只是 JS 的 mouseover 被触发。
    # 元素左上角被文字占据，取一处文字之外的像素才反映背景色。
    assert nav_shot_path is not None
    assert _png_pixel(nav_shot_path.read_bytes(), 150, 40) == (47, 111, 87)


def test_real_chrome_captures_a_single_element_even_outside_the_viewport(
    tmp_path: Path,
) -> None:
    mark_shot_path: Path | None = None

    async def scenario() -> None:
        nonlocal mark_shot_path
        runner, url = await _serve()
        try:
            async with launch_browser_toolkit(
                url, config=_config(tmp_path), task_id="pointer-e2e-shot"
            ) as toolkit:
                await toolkit.observe()

                shot = await toolkit.capture_element_screenshot(
                    locator={"strategy": "css", "value": "#mark"}, label="色块"
                )
                assert shot.success, shot.message
                assert shot.counts_as_action is False

                mark_shot_path = Path(shot.data["screenshot_path"])
                assert shot.data["box"]["width"] == 120

                # 元素在视口外，工具不滚动页面，因此页面位置保持不变。
                scrolled = await toolkit.read_element(locator={"strategy": "css", "value": "#nav"})
                assert scrolled.data["in_viewport"] is True
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

    # clip 必须落在元素上；少加滚动偏移就会截到页面顶部的空白。
    assert mark_shot_path is not None
    assert _png_pixel(mark_shot_path.read_bytes(), 20, 20) == (255, 0, 255)


def test_real_chrome_element_screenshot_is_private_and_supports_padding(
    tmp_path: Path,
) -> None:
    plain_shot_path: Path | None = None

    async def scenario() -> None:
        nonlocal plain_shot_path
        runner, url = await _serve()
        try:
            async with launch_browser_toolkit(
                url, config=_config(tmp_path), task_id="pointer-e2e-padding"
            ) as toolkit:
                await toolkit.observe()

                plain = await toolkit.capture_element_screenshot(
                    locator={"strategy": "css", "value": "#row"}, label="行"
                )
                padded = await toolkit.capture_element_screenshot(
                    locator={"strategy": "css", "value": "#row"}, label="行加边", padding=10
                )

                assert plain.success and padded.success
                plain_shot_path = Path(plain.data["screenshot_path"])
                assert padded.data["clip"]["width"] == plain.data["clip"]["width"] + 20
                assert padded.data["clip"]["x"] == plain.data["clip"]["x"] - 10
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

    # 截图可能含个人信息，落盘后只能任务自己可读。
    assert plain_shot_path is not None
    assert oct(plain_shot_path.stat().st_mode & 0o777) == "0o600"


def test_real_chrome_opens_and_closes_a_task_owned_tab(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _serve()
        try:
            async with launch_browser_toolkit(
                url, config=_config(tmp_path), task_id="pointer-e2e-tab"
            ) as toolkit:
                await toolkit.observe()
                before = (await toolkit.list_tabs()).data["tab_count"]

                opened = await toolkit.open_tab(f"{url.rstrip('/')}/detail")
                assert opened.success, opened.message
                target_id = opened.data["target_id"]

                # 打开后当前页已经切换，旧观察必须作废。
                assert toolkit.observation is None
                detail = await toolkit.observe()
                assert "/detail" in detail.url

                tabs = (await toolkit.list_tabs()).data
                assert tabs["tab_count"] == before + 1
                new_tab = next(item for item in tabs["tabs"] if item["target_id"] == target_id)
                assert new_tab["is_current"] is True
                # 任务自建的页面才允许被任务关闭。
                assert new_tab["owned_by_task"] is True

                closed = await toolkit.close_tab(target_id)
                assert closed.success, closed.message
                assert (await toolkit.list_tabs()).data["tab_count"] == before
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_refuses_to_open_a_tab_outside_the_task_scope(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _serve()
        try:
            async with launch_browser_toolkit(
                url, config=_config(tmp_path), task_id="pointer-e2e-scope"
            ) as toolkit:
                await toolkit.observe()
                before = (await toolkit.list_tabs()).data["tab_count"]

                blocked = await toolkit.open_tab("https://evil.example.net/steal")

                assert blocked.success is False
                assert (await toolkit.list_tabs()).data["tab_count"] == before
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

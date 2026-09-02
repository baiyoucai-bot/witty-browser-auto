"""用真实 Chrome 验证元素拖放、PDF 导出与性能采集。

拖放那两项的判据是**页面自己记录的结果**而不是"工具没报错"：HTML5 那页由 drop 回调
把卡片真的搬进目标列，鼠标那页由 mouseup 命中判定搬运。纯鼠标事件对 HTML5 页面只会
触发 dragstart，因此这两项必须分别断言，一项通过说明不了另一项。
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

_HTML5_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>看板</title>
<style>.col{display:inline-block;width:200px;min-height:150px;border:2px solid #333;
margin:10px;padding:8px;vertical-align:top}
.card{border:1px solid #888;padding:6px;margin:4px;background:#eee}</style></head>
<body>
<h1>看板</h1>
<div class="col" id="todo" data-testid="col-todo">待办
  <div class="card" id="card1" draggable="true" data-testid="card-1">任务甲</div>
</div>
<div class="col" id="done" data-testid="col-done">已完成</div>
<div id="state">card1@todo</div>
<script>
const card = document.getElementById('card1');
card.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', 'card1'); });
const done = document.getElementById('done');
done.addEventListener('dragover', (e) => e.preventDefault());
done.addEventListener('drop', (e) => {
  e.preventDefault();
  const id = e.dataTransfer.getData('text/plain');
  done.appendChild(document.getElementById(id));
  document.getElementById('state').textContent = id + '@done';
});
</script></body></html>"""

_MOUSE_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>排序</title>
<style>.zone{display:inline-block;width:200px;min-height:150px;border:2px dashed #666;
margin:10px;padding:8px;vertical-align:top}
.item{border:1px solid #888;padding:6px;margin:4px;background:#efe}</style></head>
<body>
<h1>排序</h1>
<div class="zone" id="left" data-testid="zone-left">左
  <div class="item" id="item1" data-testid="item-1">条目甲</div>
</div>
<div class="zone" id="right" data-testid="zone-right">右</div>
<div id="state">item1@left</div>
<script>
// 只监听鼠标事件，完全不用 HTML5 拖放 API。
let dragging = null;
document.getElementById('item1').addEventListener('mousedown', (e) => { dragging = e.target; });
document.addEventListener('mouseup', (e) => {
  if (!dragging) return;
  const hit = document.elementFromPoint(e.clientX, e.clientY);
  const zone = hit && hit.closest ? hit.closest('.zone') : null;
  if (zone && zone.id === 'right') {
    zone.appendChild(dragging);
    document.getElementById('state').textContent = dragging.id + '@right';
  }
  dragging = null;
});
</script></body></html>"""

_SLOW_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>报表</title>
<style>body{font-family:sans-serif}h1{color:#036}
.block{height:400px;background:linear-gradient(#def,#9bd);margin:12px 0}</style></head>
<body>
<h1>季度报表</h1>
<p>本页用于验证 PDF 导出与性能采集。</p>
<div class="block">区块一</div>
<div class="block">区块二</div>
<div class="block">区块三</div>
<img src="/hero.png" width="800" height="400" alt="主图">
</body></html>"""


def _config(root: Path) -> AppConfig:
    return AppConfig(
        browser=BrowserConfig(
            headless=True,
            profile_root=root / "profiles",
            command_timeout_seconds=20,
            launch_timeout_seconds=25,
        ),
        storage=StorageConfig(memory_database=root / "m.db", artifact_root=root / "artifacts"),
    )


async def _serve(body: str) -> tuple[web.AppRunner, str]:
    app = web.Application()

    async def page(_: web.Request) -> web.Response:
        return web.Response(text=body, content_type="text/html")

    async def hero(_: web.Request) -> web.Response:
        # 让 LCP 有一个明确的、需要下载时间的候选。
        await asyncio.sleep(0.25)
        return web.Response(body=b"\x89PNG\r\n\x1a\n" + b"\x00" * 120_000, content_type="image/png")

    app.router.add_get("/", page)
    app.router.add_get("/hero.png", hero)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("验收服务未返回监听端口")
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}/"


def _read_pdf(path: Path) -> tuple[str, bytes]:
    return oct(path.stat().st_mode)[-3:], path.read_bytes()


def _by_test_id(value: str) -> dict[str, str]:
    return {"strategy": "test_id", "value": value}


async def _state(toolkit) -> str:
    result = await toolkit.driver.session.call(
        "Runtime.evaluate",
        {"expression": "document.getElementById('state').textContent", "returnByValue": True},
    )
    return result.get("result", {}).get("value")


def test_html5_native_drag_actually_drops(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, base_url = await _serve(_HTML5_PAGE)
        try:
            async with launch_browser_toolkit(
                base_url,
                goal="看板换列",
                config=_config(tmp_path),
                allowed_origins=[base_url],
            ) as toolkit:
                assert await _state(toolkit) == "card1@todo"
                # 整页可拖动的都是普通 div，语义观察给不出任何候选，
                # 所以看板这类界面上定位器是唯一可行入口。
                assert not (await toolkit.observe(force=True)).candidates
                result = await toolkit.drag_to_element(
                    source_locator=_by_test_id("card-1"),
                    target_locator=_by_test_id("col-done"),
                )
                assert result.success, result.message
                # 纯鼠标事件在这一页只会触发 dragstart，必须走原生通道才会 drop。
                assert result.data["channel"] == "html5", result.data
                assert "text/plain" in result.data["mime_types"]
                assert await _state(toolkit) == "card1@done"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_mouse_driven_drag_still_works(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, base_url = await _serve(_MOUSE_PAGE)
        try:
            async with launch_browser_toolkit(
                base_url,
                goal="拖动排序",
                config=_config(tmp_path),
                allowed_origins=[base_url],
            ) as toolkit:
                assert await _state(toolkit) == "item1@left"
                result = await toolkit.drag_to_element(
                    source_locator=_by_test_id("item-1"),
                    target_locator=_by_test_id("zone-right"),
                )
                assert result.success, result.message
                # 这一页没有 draggable 元素，不该被误判成原生拖放。
                assert result.data["channel"] == "pointer", result.data
                assert await _state(toolkit) == "item1@right"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_save_pdf_writes_a_real_pdf(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, base_url = await _serve(_SLOW_PAGE)
        try:
            async with launch_browser_toolkit(
                base_url,
                goal="导出报表",
                config=_config(tmp_path),
                allowed_origins=[base_url],
            ) as toolkit:
                result = await toolkit.save_pdf(label="report", paper="a4", landscape=True)
                assert result.success, result.message
                path = Path(result.data["pdf_path"])
                mode, payload = await asyncio.to_thread(_read_pdf, path)
                assert mode == "600"
                assert payload.startswith(b"%PDF")
                assert payload.rstrip().endswith(b"%%EOF")
                assert result.data["bytes"] > 1000

                ranged = await toolkit.save_pdf(label="firstpage", page_ranges="1")
                assert ranged.success, ranged.message
                # 只导第一页必然小于整份。
                assert ranged.data["bytes"] < result.data["bytes"]
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_performance_needs_a_reload_to_report_lcp(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, base_url = await _serve(_SLOW_PAGE)
        try:
            async with launch_browser_toolkit(
                base_url,
                goal="性能采集",
                config=_config(tmp_path),
                allowed_origins=[base_url],
            ) as toolkit:
                without_reload = await toolkit.measure_performance()
                assert without_reload.success, without_reload.message
                # 采集器晚于本次导航安装，buffered 也补不回 LCP。
                assert without_reload.data["core_web_vitals"]["lcp_ms"] is None
                assert without_reload.data["ratings"]["lcp"] == "unknown"
                assert "reload=true" in without_reload.message

                with_reload = await toolkit.measure_performance(reload=True, settle_seconds=1.0)
                assert with_reload.success, with_reload.message
                vitals = with_reload.data["core_web_vitals"]
                assert vitals["lcp_ms"] is not None, with_reload.data
                assert vitals["lcp_ms"] > 0
                assert vitals["fcp_ms"] is not None
                assert vitals["ttfb_ms"] is not None
                assert with_reload.data["ratings"]["lcp"] in {"good", "needs_improvement", "poor"}
                # 导航计时与资源概览都应该有内容。
                assert with_reload.data["navigation"]["load_ms"] > 0
                assert with_reload.data["resources"]["count"] >= 1
                assert with_reload.data["counters"]["dom_nodes"] > 0
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

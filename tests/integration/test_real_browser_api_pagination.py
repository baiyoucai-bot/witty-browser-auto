"""用真实 Chrome 验证主动分页采集。

这里要证的是一句话：**页面只加载了第 1 页，数据却能一次取全。** 所以服务端会记录
自己实际被请求过哪几页，并且对缺少 Authorization 头或会话 Cookie 的请求一律拒绝——
只有这样，"取回了 87 条"才同时说明鉴权确实随重放带了过去。

另外两条断言针对最容易做糊的地方：服务端忽略分页参数时必须被识破，以及页数不够时
绝不能把"抓了一些"报成"抓全了"。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from witty_browser_auto.config import AppConfig, BrowserConfig, NetworkTrafficConfig, StorageConfig
from witty_browser_auto.toolkit import launch_browser_toolkit

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("WITTY_BROWSER_AUTO_RUN_BROWSER_TESTS") != "1",
        reason="设置 WITTY_BROWSER_AUTO_RUN_BROWSER_TESTS=1 后执行真实浏览器测试",
    ),
]

_TOKEN = "pagination-token-4b7e"
_TOTAL = 87
_PAGE_SIZE = 20

_HOME = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>分页采集验收</title></head>
<body><pre id="state">loading</pre>
<script>
const headers = {'Authorization': 'Bearer __TOKEN__', 'Accept': 'application/json'};
(async () => {
  // 页面只看第一页，剩下的交给采集器。
  await fetch('/api/orders?page=1&size=20&status=paid', {headers}).then(r => r.json());
  await fetch('/api/feed?limit=10', {headers}).then(r => r.json());
  await fetch('/api/stuck?page=1&size=10', {headers}).then(r => r.json());
  document.getElementById('state').textContent = 'done';
})();
</script>
</body></html>"""


def _rows(start: int, end: int) -> list[dict[str, Any]]:
    return [
        {"id": 1000 + index, "buyer": f"买家{index}", "amount": round(10 + index * 1.5, 2)}
        for index in range(start, end)
    ]


async def _serve(seen: dict[str, list[str]]) -> tuple[web.AppRunner, str]:
    app = web.Application()

    def guard(request: web.Request) -> web.Response | None:
        if request.headers.get("Authorization") != f"Bearer {_TOKEN}":
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.cookies.get("sid") != "session-abc":
            return web.json_response({"error": "no session"}, status=403)
        return None

    async def home(_: web.Request) -> web.Response:
        response = web.Response(text=_HOME.replace("__TOKEN__", _TOKEN), content_type="text/html")
        response.set_cookie("sid", "session-abc", path="/")
        return response

    async def orders(request: web.Request) -> web.Response:
        denied = guard(request)
        if denied is not None:
            return denied
        page = int(request.query.get("page", "1"))
        size = int(request.query.get("size", str(_PAGE_SIZE)))
        seen["orders"].append(str(page))
        offset = (page - 1) * size
        return web.json_response(
            {"total": _TOTAL, "data": {"items": _rows(offset, min(offset + size, _TOTAL))}}
        )

    async def feed(request: web.Request) -> web.Response:
        denied = guard(request)
        if denied is not None:
            return denied
        cursor = request.query.get("cursor") or "0"
        seen["feed"].append(cursor)
        offset = int(cursor)
        limit = int(request.query.get("limit", "10"))
        window = _rows(offset, min(offset + limit, 25))
        following = offset + limit
        return web.json_response(
            {
                "items": window,
                "paging": {"next": str(following) if following < 25 else None},
            }
        )

    async def stuck(request: web.Request) -> web.Response:
        denied = guard(request)
        if denied is not None:
            return denied
        seen["stuck"].append(request.query.get("page", "1"))
        # 无论请求第几页都返回同一批：模拟参数名猜错或服务端不认这个参数。
        return web.json_response({"items": _rows(0, 10)})

    app.router.add_get("/", home)
    app.router.add_get("/api/orders", orders)
    app.router.add_get("/api/feed", feed)
    app.router.add_get("/api/stuck", stuck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("分页采集验收服务未返回监听端口")
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}"


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
        traffic=NetworkTrafficConfig(
            body_resource_types=("XHR", "Fetch", "Document", "Other"),
        ),
    )


async def _wait_for_exchange(toolkit: Any, url_contains: str) -> None:
    deadline = asyncio.get_running_loop().time() + 15.0
    while asyncio.get_running_loop().time() < deadline:
        result = await toolkit.inspect_network_traffic(url_contains=url_contains)
        if any(item["state"] == "finished" for item in result.data["exchanges"]):
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"等待流量交换超时：{url_contains}")


def test_one_captured_page_is_enough_to_collect_them_all(tmp_path: Path) -> None:
    seen: dict[str, list[str]] = {"orders": [], "feed": [], "stuck": []}

    async def scenario() -> None:
        runner, base = await _serve(seen)
        try:
            async with launch_browser_toolkit(
                f"{base}/", config=_config(tmp_path), allowed_origins=[base]
            ) as toolkit:
                await _wait_for_exchange(toolkit, "/api/orders")
                assert seen["orders"] == ["1"], "页面本身只应该请求第一页"

                result = await toolkit.collect_api_pages(url_contains="/api/orders")

                assert result.success, result.message
                data = result.data
                assert data["closed"] is True
                assert data["collected"] == _TOTAL
                assert data["declared_total"] == _TOTAL
                assert len(data["records"]) == _TOTAL
                # 记录必须按页序拼接，不能靠去重掩盖乱序。
                assert [row["id"] for row in data["records"]] == [
                    1000 + index for index in range(_TOTAL)
                ]
                # 87 条按每页 20 条走满 5 页，且确实打到了服务端。
                assert data["pages_fetched"] == 5
                assert seen["orders"] == ["1", "1", "2", "3", "4", "5"]
                assert data["plan"]["strategy"] == "page_number"
                assert data["plan"]["start"] == 1

                # 业务数据只回给调用方，模型侧只见计数与闭合结论。
                assert "records" not in result.model_data
                assert result.model_data["collected"] == _TOTAL
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_cursor_feed_walks_until_the_server_stops_handing_out_cursors(tmp_path: Path) -> None:
    seen: dict[str, list[str]] = {"orders": [], "feed": [], "stuck": []}

    async def scenario() -> None:
        runner, base = await _serve(seen)
        try:
            async with launch_browser_toolkit(
                f"{base}/", config=_config(tmp_path), allowed_origins=[base]
            ) as toolkit:
                await _wait_for_exchange(toolkit, "/api/feed")

                result = await toolkit.collect_api_pages(
                    url_contains="/api/feed",
                    strategy="cursor",
                    page_param="cursor",
                    cursor_field="next",
                )

                assert result.success, result.message
                data = result.data
                assert data["closed"] is True
                assert data["collected"] == 25
                assert seen["feed"] == ["0", "0", "10", "20"]
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_a_server_that_ignores_the_page_parameter_is_reported_not_hidden(tmp_path: Path) -> None:
    seen: dict[str, list[str]] = {"orders": [], "feed": [], "stuck": []}

    async def scenario() -> None:
        runner, base = await _serve(seen)
        try:
            async with launch_browser_toolkit(
                f"{base}/", config=_config(tmp_path), allowed_origins=[base]
            ) as toolkit:
                await _wait_for_exchange(toolkit, "/api/stuck")

                result = await toolkit.collect_api_pages(url_contains="/api/stuck")

                # 拿到 10 条也不算成功：这批数据根本不完整。
                assert result.success is False
                data = result.data
                assert data["closed"] is False
                assert "忽略了分页参数" in data["failed_pages"][0]["error"]
                # 第二页就该收手，不该把 50 页额度耗光。
                assert data["pages_fetched"] == 2
                assert len(seen["stuck"]) == 3
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_an_unfinished_walk_never_claims_completeness(tmp_path: Path) -> None:
    seen: dict[str, list[str]] = {"orders": [], "feed": [], "stuck": []}

    async def scenario() -> None:
        runner, base = await _serve(seen)
        try:
            async with launch_browser_toolkit(
                f"{base}/", config=_config(tmp_path), allowed_origins=[base]
            ) as toolkit:
                await _wait_for_exchange(toolkit, "/api/orders")

                result = await toolkit.collect_api_pages(url_contains="/api/orders", max_pages=2)

                assert result.success is False
                data = result.data
                assert data["closed"] is False
                assert data["collected"] == 40
                assert data["declared_total"] == _TOTAL
                assert "相差 47 条" in data["reason"]
                # 已抓到的记录仍然交还，调用方可以据此续采。
                assert len(data["records"]) == 40
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

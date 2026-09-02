"""用真实 Chrome 验证抓包式流量检查、HAR 导出与请求重放。

这一批能力全部依赖真实的 CDP 事件时序和浏览器网络栈，假驱动证伪不了：
`Network.requestWillBeSentExtraInfo` 才带得上 Cookie，`ResourceTiming` 只有真实
连接才有值，而重放里最关键的一条——`fetch` 拒绝设置 `Cookie`、必须靠一次性 Fetch
拦截补齐——只有让服务端把收到的 Header 回显出来才能证明。
"""

from __future__ import annotations

import asyncio
import json
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

_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>流量验收</title></head>
<body>
  <p id="status">未加载</p>
  <link rel="stylesheet" href="/static/app.css">
  <script>
    window.__ordersDone = false;
    async function loadOrders() {
      const response = await fetch('/api/orders?page=1', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Trace': 'page-load'},
        body: JSON.stringify({page: 1, size: 10}),
      });
      const data = await response.json();
      document.querySelector('#status').textContent = '已加载:' + data.total;
      window.__ordersDone = true;
    }
    async function callMissing() {
      try { await fetch('/api/missing'); } catch (error) { /* 状态码由流量日志记录 */ }
    }
    loadOrders();
    callMissing();
  </script>
</body></html>"""


async def _serve(host: str) -> tuple[web.AppRunner, int, list[dict[str, Any]]]:
    """回显收到的 Header 与请求体，让重放能被服务端侧证明。"""

    received: list[dict[str, Any]] = []

    async def page(_request: web.Request) -> web.Response:
        response = web.Response(text=_PAGE, content_type="text/html")
        response.set_cookie("session", "browser-session", path="/")
        return response

    async def orders(request: web.Request) -> web.Response:
        body = await request.text()
        received.append(
            {
                "path": str(request.rel_url),
                "method": request.method,
                "cookie": request.headers.get("Cookie", ""),
                "trace": request.headers.get("X-Trace", ""),
                "if_none_match": request.headers.get("If-None-Match", ""),
                "body": body,
            }
        )
        payload = {"total": 2, "echo": body, "records": [{"id": 1}, {"id": 2}]}
        return web.Response(
            text=json.dumps(payload),
            content_type="application/json",
            headers={"X-Server-Trace": "srv-1", "ETag": 'W/"v1"'},
        )

    async def missing(_request: web.Request) -> web.Response:
        return web.Response(status=404, text='{"error":"not found"}', content_type="text/plain")

    async def stylesheet(_request: web.Request) -> web.Response:
        return web.Response(text="body{margin:0}", content_type="text/css")

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_route("*", "/api/orders", orders)
    app.router.add_get("/api/missing", missing)
    app.router.add_get("/static/app.css", stylesheet)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("流量验收服务未返回监听端口")
    return runner, int(site._server.sockets[0].getsockname()[1]), received


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
            body_resource_types=("XHR", "Fetch", "Document", "Stylesheet", "Other"),
        ),
    )


async def _wait_for_exchange(toolkit: Any, url_contains: str, *, wait_seconds: float = 15.0) -> Any:
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while asyncio.get_running_loop().time() < deadline:
        result = await toolkit.inspect_network_traffic(url_contains=url_contains)
        finished = [
            item for item in result.data["exchanges"] if item["state"] in {"finished", "failed"}
        ]
        if finished:
            return finished[-1]
        await asyncio.sleep(0.2)
    raise AssertionError(f"等待流量交换超时：{url_contains}")


def test_real_chrome_records_full_traffic_and_exports_har(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, port, _received = await _serve("127.0.0.1")
        base = f"http://127.0.0.1:{port}"
        try:
            async with launch_browser_toolkit(
                f"{base}/",
                config=_config(tmp_path),
                allowed_origins=[base],
            ) as toolkit:
                orders = await _wait_for_exchange(toolkit, "/api/orders")

                assert orders["method"] == "POST"
                assert orders["url"] == f"{base}/api/orders?page=1"
                assert orders["status"] == 200
                assert orders["resource_type"] in {"XHR", "Fetch"}
                assert orders["state"] == "finished"

                # 请求头必须来自 extraInfo，否则拿不到浏览器实际附带的 Cookie。
                request_headers = {
                    name.casefold(): value for name, value in orders["request_headers"].items()
                }
                assert request_headers["x-trace"] == "page-load"
                assert "browser-session" in request_headers["cookie"]

                response_headers = {
                    name.casefold(): value for name, value in orders["response_headers"].items()
                }
                assert response_headers["x-server-trace"] == "srv-1"

                assert orders["timing"]["total_ms"] is not None
                assert orders["timing"]["wait_ms"] is not None
                assert orders["initiator"]["type"] in {"script", "parser", "other"}
                assert orders["remote_address"] in {"127.0.0.1", "::1"}

                body = await toolkit.read_network_body(orders["exchange_id"])
                assert body.data["json"]["total"] == 2
                request_body = await toolkit.read_network_body(
                    orders["exchange_id"], part="request"
                )
                assert json.loads(request_body.data["text"]) == {"page": 1, "size": 10}

                failed = await _wait_for_exchange(toolkit, "/api/missing")
                assert failed["status"] == 404

                errors = await toolkit.inspect_network_traffic(status_min=400, status_max=599)
                error_ids = {item["exchange_id"] for item in errors.data["exchanges"]}
                assert failed["exchange_id"] in error_ids
                assert all(400 <= item["status"] <= 599 for item in errors.data["exchanges"])

                stylesheet = await _wait_for_exchange(toolkit, "/static/app.css")
                assert stylesheet["resource_type"] == "Stylesheet"

                har = await toolkit.export_network_har("流量验收", url_contains="/api/")
                exported["path"] = har.data["har_path"]
                exported["base"] = base

                # 模型侧视图必须看不到正文和 Header 值。
                traffic = await toolkit.inspect_network_traffic(url_contains="/api/orders")
                model_text = json.dumps(traffic.model_data, ensure_ascii=False)
                assert "browser-session" not in model_text
                assert "srv-1" not in model_text
        finally:
            await runner.cleanup()

    exported: dict[str, str] = {}
    asyncio.run(scenario())

    path = Path(exported["path"])
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["log"]["version"] == "1.2"
    urls = {entry["request"]["url"] for entry in document["log"]["entries"]}
    assert f"{exported['base']}/api/orders?page=1" in urls
    order_entry = next(
        entry
        for entry in document["log"]["entries"]
        if entry["request"]["url"].endswith("/api/orders?page=1")
    )
    assert order_entry["response"]["content"]["text"].startswith('{"total": 2')
    assert order_entry["timings"]["wait"] >= 0


def test_real_chrome_replays_request_with_edited_body_and_restricted_headers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runner, port, received = await _serve("127.0.0.1")
        base = f"http://127.0.0.1:{port}"
        try:
            async with launch_browser_toolkit(
                f"{base}/",
                config=_config(tmp_path),
                allowed_origins=[base],
            ) as toolkit:
                orders = await _wait_for_exchange(toolkit, "/api/orders")
                received.clear()

                plain = await toolkit.replay_network_request(exchange_id=orders["exchange_id"])
                assert plain.success is True
                assert plain.data["status"] == 200
                assert plain.data["json"]["total"] == 2
                assert plain.data["headers"]["x-server-trace"] == "srv-1"
                assert json.loads(plain.data["json"]["echo"]) == {"page": 1, "size": 10}
                assert received[-1]["method"] == "POST"
                assert "browser-session" in received[-1]["cookie"]

                edited = await toolkit.replay_network_request(
                    exchange_id=orders["exchange_id"],
                    body='{"page": 2, "size": 50}',
                    headers={"X-Trace": "replayed", "Cookie": "session=forged-by-replay"},
                    remove_headers=["If-None-Match"],
                )
                assert edited.success is True
                assert edited.data["status"] == 200
                seen = received[-1]
                assert json.loads(seen["body"]) == {"page": 2, "size": 50}
                assert seen["trace"] == "replayed"
                # fetch 不允许脚本设置 Cookie，这一条通过说明一次性 Fetch 拦截真的生效了。
                assert seen["cookie"] == "session=forged-by-replay"
                assert seen["if_none_match"] == ""

                # 重放本身也会进流量日志，并标记来源交换。
                replayed = await _wait_for_exchange(toolkit, "/api/orders")
                assert replayed["exchange_id"] != orders["exchange_id"]

                # 拦截必须在重放结束后撤销，否则后续请求会被继续改写。
                after = await toolkit.replay_network_request(
                    exchange_id=orders["exchange_id"],
                    headers={"X-Trace": "third"},
                )
                assert after.success is True
                assert received[-1]["trace"] == "third"
                assert "browser-session" in received[-1]["cookie"]

                model_text = json.dumps(edited.model_data, ensure_ascii=False)
                assert "forged-by-replay" not in model_text
                assert "srv-1" not in model_text
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_rejects_replay_outside_allowed_origins(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, port, _received = await _serve("127.0.0.1")
        base = f"http://127.0.0.1:{port}"
        try:
            async with launch_browser_toolkit(
                f"{base}/",
                config=_config(tmp_path),
                allowed_origins=[base],
            ) as toolkit:
                await _wait_for_exchange(toolkit, "/api/orders")
                result = await toolkit.replay_network_request(url="https://example.org/api")
                assert result.success is False
                assert "origin" in result.message
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

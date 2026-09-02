"""用真实 Chrome 验证 WebSocket 帧读取。

WebSocket 帧既不是请求体也不是响应体，`read_network_body` 对这类交换必然落空，
所以这条路径必须单独验收：真实浏览器建连、双向收发之后，帧内容要能原样取回。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from aiohttp import WSMsgType, web

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
<html lang="zh-CN"><head><meta charset="utf-8"><title>WebSocket 验收</title></head>
<body><pre id="state">connecting</pre>
<script>
const socket = new WebSocket(`ws://${location.host}/stream`);
let received = 0;
socket.addEventListener('open', () => {
  socket.send(JSON.stringify({ op: 'subscribe', channel: 'ticker' }));
});
socket.addEventListener('message', (event) => {
  received += 1;
  if (received >= 3) {
    document.getElementById('state').textContent = 'done';
  }
});
</script>
</body></html>"""


async def _serve() -> tuple[web.AppRunner, str]:
    app = web.Application()

    async def home(_: web.Request) -> web.Response:
        return web.Response(text=_PAGE, content_type="text/html")

    async def stream(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            if message.type is not WSMsgType.TEXT:
                break
            for index in range(3):
                await socket.send_str(
                    json.dumps({"channel": "ticker", "seq": index, "price": 100 + index})
                )
            break
        return socket

    app.router.add_get("/", home)
    app.router.add_get("/stream", stream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("WebSocket 验收服务未返回监听端口")
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
        traffic=NetworkTrafficConfig(),
    )


async def _wait_for_frames(toolkit: Any, *, minimum: int) -> dict:
    deadline = asyncio.get_running_loop().time() + 15.0
    while asyncio.get_running_loop().time() < deadline:
        traffic = await toolkit.inspect_network_traffic(url_contains="/stream")
        for item in traffic.data["exchanges"]:
            websocket = item.get("websocket")
            if websocket and websocket["frame_count"] >= minimum:
                return item
        await asyncio.sleep(0.2)
    raise AssertionError("等待 WebSocket 帧超时")


def test_real_chrome_reads_websocket_frames_both_directions(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, base = await _serve()
        try:
            async with launch_browser_toolkit(
                f"{base}/",
                config=_config(tmp_path),
                allowed_origins=[base],
            ) as toolkit:
                exchange = await _wait_for_frames(toolkit, minimum=4)
                exchange_id = exchange["exchange_id"]
                assert exchange["resource_type"] == "WebSocket"

                # 流量清单本身不含帧正文，这正是本工具存在的理由。
                listing = json.dumps(exchange, ensure_ascii=False)
                assert "subscribe" not in listing

                # read_network_body 对 WebSocket 交换没有可读正文，这正是本工具存在的理由。
                body = await toolkit.read_network_body(exchange_id)
                assert body.success is False
                assert "正文" in body.message

                frames = await toolkit.read_websocket_frames(exchange_id)
                assert frames.success, frames.message
                data = frames.data
                assert data["directions"]["sent"] >= 1
                assert data["directions"]["received"] >= 3

                sent = await toolkit.read_websocket_frames(exchange_id, direction="sent")
                assert sent.data["returned_count"] == 1
                assert sent.data["frames"][0]["json"] == {
                    "op": "subscribe",
                    "channel": "ticker",
                }

                received = await toolkit.read_websocket_frames(
                    exchange_id, direction="received", contains="price"
                )
                prices = [item["json"]["price"] for item in received.data["frames"]]
                assert prices == [100, 101, 102]

                newest = await toolkit.read_websocket_frames(
                    exchange_id, direction="received", limit=1
                )
                assert newest.data["returned_count"] == 1
                assert newest.data["frames"][0]["json"]["seq"] == 2

                # 模型侧只拿统计，拿不到帧正文。
                model_text = json.dumps(frames.model_data, ensure_ascii=False)
                assert "subscribe" not in model_text
                assert "price" not in model_text
                assert frames.model_data["frame_count"] >= 4
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

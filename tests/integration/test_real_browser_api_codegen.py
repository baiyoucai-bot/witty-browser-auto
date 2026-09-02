"""用真实 Chrome 验证接口剖析，并把导出的代码真正执行一遍。

代码生成器最容易出的问题不是"生成不出来"，而是"生成出来跑不通"：漏掉鉴权头、
把浏览器自动管理的 Header 照抄进去、正文编码方式选错。所以这里不止断言字符串，
而是把导出的 curl 与 Node 代码交给真实进程去请求同一个服务端。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
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

_TOKEN = "demo-token-9f3a"
_API_KEY = "key-77123"

_HOME = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>接口剖析验收</title></head>
<body><pre id="state">loading</pre>
<script>
const call = (page) => fetch(`/api/orders?page=${page}&size=20&status=paid`, {
  headers: {
    'Authorization': 'Bearer __TOKEN__',
    'X-Api-Key': '__API_KEY__',
    'Accept': 'application/json',
  },
}).then(r => r.json());
(async () => {
  await call(1);
  await call(2);
  document.getElementById('state').textContent = 'done';
})();
</script>
</body></html>"""

_PAYLOAD = {
    "total": 87,
    "hasMore": True,
    "data": [
        {"id": 1001, "buyer": "张三", "amount": 128.5, "phone": "13800000000"},
        {"id": 1002, "buyer": "李四", "amount": 64.0, "phone": "13900000000"},
    ],
}


async def _serve() -> tuple[web.AppRunner, str]:
    app = web.Application()

    async def home(_: web.Request) -> web.Response:
        body = _HOME.replace("__TOKEN__", _TOKEN).replace("__API_KEY__", _API_KEY)
        return web.Response(text=body, content_type="text/html")

    async def orders(request: web.Request) -> web.Response:
        # 鉴权头缺失时返回 401：这样"导出的代码能跑通"才是有意义的断言。
        if request.headers.get("Authorization") != f"Bearer {_TOKEN}":
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.headers.get("X-Api-Key") != _API_KEY:
            return web.json_response({"error": "missing api key"}, status=403)
        return web.json_response(_PAYLOAD)

    app.router.add_get("/", home)
    app.router.add_get("/api/orders", orders)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("接口剖析验收服务未返回监听端口")
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


async def _wait_for_exchange(toolkit: Any, url_contains: str, *, count: int = 1) -> list[dict]:
    deadline = asyncio.get_running_loop().time() + 15.0
    while asyncio.get_running_loop().time() < deadline:
        result = await toolkit.inspect_network_traffic(url_contains=url_contains)
        finished = [item for item in result.data["exchanges"] if item["state"] == "finished"]
        if len(finished) >= count:
            return finished
        await asyncio.sleep(0.2)
    raise AssertionError(f"等待流量交换超时：{url_contains}")


def test_real_chrome_analyzes_endpoint_and_exports_runnable_code(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def scenario() -> None:
        runner, base = await _serve()
        try:
            async with launch_browser_toolkit(
                f"{base}/",
                config=_config(tmp_path),
                allowed_origins=[base],
            ) as toolkit:
                exchanges = await _wait_for_exchange(toolkit, "/api/orders", count=2)
                assert all(item["status"] == 200 for item in exchanges)

                # ---- 接口契约剖析 ----
                analysis = await toolkit.analyze_api_endpoint(url_contains="/api/orders")
                assert analysis.success, analysis.message
                data = analysis.data

                assert data["endpoint"]["method"] == "GET"
                assert data["endpoint"]["url_template"] == f"{base}/api/orders"
                assert data["sample_count"] == 2

                params = {item["name"]: item for item in data["query_params"]}
                assert params["page"]["role"] == "pagination"
                assert params["page"]["varies"] is True
                assert params["size"]["varies"] is False
                assert params["status"]["always_present"] is True
                assert data["pagination"]["strategy"] == "page_number"
                assert "page" in data["pagination"]["request_params"]

                assert data["auth"]["authorization_schemes"] == ["Bearer"]
                assert "X-Api-Key" in data["auth"]["credential_headers"]

                response = data["response"]
                assert response["record_path"] == ["data"]
                assert response["record_count"] == 2
                assert response["total_fields"] == ["total"]
                assert "hasMore" in response["pagination_fields"]
                assert set(response["record_fields"]) == {"id", "buyer", "amount", "phone"}

                # 模型侧不得看到业务取值与凭据。
                model_text = json.dumps(analysis.model_data, ensure_ascii=False)
                assert "张三" not in model_text
                assert "13800000000" not in model_text
                assert _TOKEN not in model_text
                assert _API_KEY not in model_text

                exchange_id = data["sample_exchange_id"]

                # ---- 代码导出：默认凭据占位 ----
                masked = await toolkit.export_request_code(exchange_id, target="curl")
                assert masked.success
                assert _TOKEN not in masked.data["code"]
                envs = {item["env"] for item in masked.data["placeholders"]}
                assert "AUTHORIZATION" in envs
                captured["curl_masked"] = masked.data["code"]
                captured["curl_env"] = {
                    item["env"]: dict(exchanges[-1]["request_headers"])[item["header"]]
                    for item in masked.data["placeholders"]
                }

                inlined = await toolkit.export_request_code(
                    exchange_id, target="curl", include_secrets=True
                )
                captured["curl_inlined"] = inlined.data["code"]

                node_code = await toolkit.export_request_code(
                    exchange_id, target="javascript_fetch", include_secrets=True
                )
                captured["node"] = node_code.data["code"]

                python_code = await toolkit.export_request_code(
                    exchange_id, target="python_requests"
                )
                captured["python"] = python_code.data["code"]

            # 浏览器已关闭，但服务端仍在监听：导出的代码必须脱离浏览器独立跑通。
            await asyncio.to_thread(_run_generated_code, captured, tmp_path)
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
    assert captured["curl_inlined_total"] == 87
    assert captured["curl_masked_total"] == 87


def _run_generated_code(captured: dict[str, Any], tmp_path: Path) -> None:
    inlined = subprocess.run(
        captured["curl_inlined"],
        shell=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert inlined.returncode == 0, inlined.stderr
    captured["curl_inlined_total"] = json.loads(inlined.stdout)["total"]

    # 占位版本必须在环境变量注入后同样跑通，否则占位方案只是好看。
    masked = subprocess.run(
        captured["curl_masked"],
        shell=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **captured["curl_env"]},
    )
    assert masked.returncode == 0, masked.stderr
    captured["curl_masked_total"] = json.loads(masked.stdout)["total"]

    # 生成的 Python 至少必须是合法语法；requests 不是本项目运行依赖，不强制执行。
    compile(captured["python"], "<generated>", "exec")

    if shutil.which("node"):
        script = tmp_path / "generated.mjs"
        script.write_text(captured["node"], encoding="utf-8")
        node_result = subprocess.run(
            ["node", str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert node_result.returncode == 0, node_result.stderr
        assert json.loads(node_result.stdout)["total"] == 87

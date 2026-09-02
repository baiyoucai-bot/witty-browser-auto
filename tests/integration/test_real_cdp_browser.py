from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from aiohttp import web

from witty_browser_auto.browser.driver import CdpAutomationDriver
from witty_browser_auto.config import BrowserConfig, NetworkCaptureConfig
from witty_browser_auto.demo_server import create_app
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionKind,
    DragPoint,
    DragRiskClass,
    ExpectedCondition,
    LocatorRecipe,
    VisualDragPoint,
)
from witty_browser_auto.network.capture import CdpNetworkCapture

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("WITTY_BROWSER_AUTO_RUN_BROWSER_TESTS") != "1",
        reason="设置 WITTY_BROWSER_AUTO_RUN_BROWSER_TESTS=1 后执行真实浏览器测试",
    ),
]


async def _start_demo_server() -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(create_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("本地验收服务未返回监听端口")
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}/"


async def _start_new_tab_server() -> tuple[web.AppRunner, str]:
    app = web.Application()

    async def home(request: web.Request) -> web.Response:
        return web.Response(
            text=(
                '<!doctype html><html><body><a href="/merchant" target="_blank">'
                "商家中心</a></body></html>"
            ),
            content_type="text/html",
        )

    async def merchant(request: web.Request) -> web.Response:
        return web.Response(
            text="<!doctype html><html><body>商家登录页</body></html>",
            content_type="text/html",
        )

    app.router.add_get("/", home)
    app.router.add_get("/merchant", merchant)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("新标签页验收服务未返回监听端口")
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}/"


async def _start_network_route_server() -> tuple[web.AppRunner, str]:
    app = web.Application()

    async def home(request: web.Request) -> web.Response:
        return web.Response(
            text=(
                "<!doctype html><html><body>"
                '<button id="load">加载</button><output id="result"></output>'
                "<script>document.querySelector('#load').onclick=async()=>{"
                "const data=await fetch('/api/value').then(response=>response.json());"
                "document.querySelector('#result').textContent=data.value;};</script>"
                "</body></html>"
            ),
            content_type="text/html",
        )

    async def value(request: web.Request) -> web.Response:
        sensitive_headers = "|".join(
            (
                request.headers.get("Authorization", ""),
                request.headers.get("Cookie", ""),
                request.headers.get("Host", ""),
            )
        )
        return web.json_response({"value": sensitive_headers or "original"})

    app.router.add_get("/", home)
    app.router.add_get("/api/value", value)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("网络路由验收服务未返回监听端口")
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}/"


def test_real_chrome_input_click_network_and_screenshot(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _start_demo_server()
        artifact_root = tmp_path / "artifacts"
        capture = CdpNetworkCapture(
            NetworkCaptureConfig(),
            artifact_root,
            allowed_origins=(url,),
        )
        driver = CdpAutomationDriver(
            BrowserConfig(
                headless=True,
                profile_root=tmp_path / "profiles",
                command_timeout_seconds=10,
                launch_timeout_seconds=20,
            ),
            artifact_root,
            network_capture=capture,
        )
        try:
            await driver.open(url)
            observation = await driver.observe(force=True)
            textbox = next(
                item
                for item in observation.candidates
                if item.role == "textbox" and item.name == "名称"
            )
            input_receipt = await driver.execute(
                ActionCommand(
                    "input-name",
                    ActionKind.INPUT_TEXT,
                    target_id=textbox.target_id,
                    value="测试姓名",
                    idempotent=True,
                )
            )
            assert input_receipt.success, input_receipt.message

            observation = await driver.observe(force=True)
            button = next(
                item
                for item in observation.candidates
                if item.role == "button" and item.name == "提交"
            )
            click_receipt = await driver.execute(
                ActionCommand(
                    "click-submit",
                    ActionKind.CLICK,
                    target_id=button.target_id,
                    expected=ExpectedCondition("text_contains", "完成：测试姓名", 5),
                )
            )
            assert click_receipt.success, click_receipt.message
            verification = await driver.verify(
                ExpectedCondition("text_contains", "完成：测试姓名", 5)
            )
            assert verification.success, verification.reason

            network = await driver.network_snapshot()
            serialized_network = str(network)
            assert "demo-secret" not in serialized_network
            assert any("/api/submit" in item["url"] for item in network)

            page_diagnostics = await driver.diagnostic_snapshot()
            assert page_diagnostics["page"]["readyState"] == "complete"
            assert page_diagnostics["environment"]["webdriver"] is False
            assert page_diagnostics["network"]["recordCount"] >= 2
            assert "demo-secret" not in str(page_diagnostics)

            inspection: dict[str, Any] = {"candidates": []}
            for _ in range(40):
                inspection = await capture.inspect()
                if inspection["candidates"]:
                    break
                await asyncio.sleep(0.05)
            candidate = inspection["candidates"][0]
            assert candidate["endpoint"].endswith("/api/submit")
            assert "demo-secret" not in str(inspection)
            export = await capture.export(candidate["candidate_id"], "本地提交响应")
            assert export.json_path.stat().st_mode & 0o777 == 0o600
            assert json.loads(export.json_path.read_text(encoding="utf-8"))["name"] == "测试姓名"

            screenshot = await driver.capture_evidence("real-cdp")
            assert screenshot.is_file()
            assert screenshot.stat().st_size > 0
        finally:
            await driver.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_explicit_locator_and_network_route(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _start_network_route_server()
        capture = CdpNetworkCapture(
            NetworkCaptureConfig(),
            tmp_path / "artifacts-route",
            allowed_origins=(url,),
        )
        driver = CdpAutomationDriver(
            BrowserConfig(
                headless=True,
                profile_root=tmp_path / "profiles-route",
                command_timeout_seconds=10,
                launch_timeout_seconds=20,
            ),
            tmp_path / "artifacts-route",
            network_capture=capture,
        )
        try:
            await driver.open(url)
            await capture.manage_route(
                "add",
                {
                    "url_pattern": f"{url.rstrip('/')}/api/value",
                    "action": "mock_response",
                    "response_headers": {"Content-Type": "application/json"},
                    "response_body": '{"value":"mocked"}',
                },
            )
            receipt = await driver.execute(
                ActionCommand(
                    "explicit-load",
                    ActionKind.CLICK,
                    locator=LocatorRecipe(
                        "explicit_css",
                        '{"value":"button#load","name":"","exact":true,'
                        '"index":0,"index_explicit":false,"timeout_seconds":3}',
                    ),
                    expected=ExpectedCondition("text_contains", "mocked", 5),
                )
            )

            assert receipt.success, receipt.message
            verification = await driver.verify(ExpectedCondition("text_contains", "mocked", 5))
            assert verification.success, verification.reason
            routes = await capture.manage_route("list", {})
            assert routes["rules"][0]["action"] == "mock_response"
            assert routes["rules"][0]["response_body_bytes"] > 0
            assert "mocked" not in str(routes)
        finally:
            await driver.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_sensitive_request_header_route(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _start_network_route_server()
        port = urlsplit(url).port
        assert port is not None
        rewritten_host = f"localhost:{port}"
        capture = CdpNetworkCapture(
            NetworkCaptureConfig(),
            tmp_path / "artifacts-sensitive-route",
            allowed_origins=(url,),
        )
        driver = CdpAutomationDriver(
            BrowserConfig(
                headless=True,
                profile_root=tmp_path / "profiles-sensitive-route",
                command_timeout_seconds=10,
                launch_timeout_seconds=20,
            ),
            tmp_path / "artifacts-sensitive-route",
            network_capture=capture,
        )
        try:
            await driver.open(url)
            await capture.manage_route(
                "add",
                {
                    "url_pattern": f"{url.rstrip('/')}/api/value",
                    "action": "modify_request",
                    "request_headers": {
                        "Authorization": "Bearer route-auth",
                        "Cookie": "session=route-cookie",
                        "Host": rewritten_host,
                    },
                },
            )
            receipt = await driver.execute(
                ActionCommand(
                    "sensitive-header-load",
                    ActionKind.CLICK,
                    locator=LocatorRecipe(
                        "explicit_css",
                        '{"value":"button#load","name":"","exact":true,'
                        '"index":0,"index_explicit":false,"timeout_seconds":3}',
                    ),
                    expected=ExpectedCondition("text_contains", "route-cookie", 5),
                )
            )

            assert receipt.success, receipt.message
            routes = await capture.manage_route("list", {})
            serialized = str(routes)
            assert set(routes["rules"][0]["request_header_names"]) == {
                "Authorization",
                "Cookie",
                "Host",
            }
            assert "route-auth" not in serialized
            assert "route-cookie" not in serialized
            assert routes["rules"][0]["request_url_host_rewrite"] is True
            verification = await driver.verify(
                ExpectedCondition("text_contains", rewritten_host, 5)
            )
            assert verification.success, verification.reason
        finally:
            await driver.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_adopts_target_blank_page_before_verification(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _start_new_tab_server()
        driver = CdpAutomationDriver(
            BrowserConfig(
                headless=True,
                profile_root=tmp_path / "profiles-new-tab",
                command_timeout_seconds=10,
                launch_timeout_seconds=20,
            ),
            tmp_path / "artifacts-new-tab",
        )
        try:
            await driver.open(url)
            observation = await driver.observe(force=True)
            merchant = next(
                item
                for item in observation.candidates
                if item.role == "link" and item.name == "商家中心"
            )
            locator = json.loads(merchant.recipe.value or "{}")
            assert locator["attrs"]["target"] == "_blank"

            receipt = await driver.execute(
                ActionCommand(
                    "open-merchant",
                    ActionKind.CLICK,
                    target_id=merchant.target_id,
                )
            )
            verification = await driver.verify(ExpectedCondition("text_contains", "商家登录页", 5))

            assert receipt.success, receipt.message
            assert receipt.data["new_page"] is True
            assert verification.success, verification.reason
            assert driver.session is not None
            assert driver.session.target_id != observation.surface_id
        finally:
            await driver.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_drag_business_slider(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _start_demo_server()
        driver = CdpAutomationDriver(
            BrowserConfig(
                headless=True,
                profile_root=tmp_path / "profiles-drag",
                command_timeout_seconds=10,
                launch_timeout_seconds=20,
            ),
            tmp_path / "artifacts-drag",
        )
        try:
            await driver.open(url)
            observation = await driver.observe(force=True)
            slider = next(
                item
                for item in observation.candidates
                if item.role == "slider" and item.name == "进度"
            )
            assert slider.drag_risk is DragRiskClass.BUSINESS
            trajectory = tuple(DragPoint(index * 8, 0, 10 if index else 0) for index in range(11))

            receipt = await driver.execute(
                ActionCommand(
                    "drag-progress",
                    ActionKind.DRAG,
                    target_id=slider.target_id,
                    trajectory=trajectory,
                    drag_risk=slider.drag_risk,
                    expected=ExpectedCondition("text_contains", "进度已调整", 5),
                )
            )
            verification = await driver.verify(ExpectedCondition("text_contains", "进度已调整", 5))

            assert receipt.success, receipt.message
            assert receipt.data["执行方式"] == "native_range"
            assert receipt.data["原值"] == "50"
            assert receipt.data["回读值"] == "100"
            assert verification.success, verification.reason
        finally:
            await driver.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_chrome_visual_drag_uses_observation_fingerprint(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, url = await _start_demo_server()
        driver = CdpAutomationDriver(
            BrowserConfig(
                headless=True,
                profile_root=tmp_path / "profiles-visual-drag",
                command_timeout_seconds=10,
                launch_timeout_seconds=20,
            ),
            tmp_path / "artifacts-visual-drag",
        )
        try:
            await driver.open(url)
            observation = await driver.observe(force=True)
            session = driver.session
            assert session is not None
            geometry_result = await session.call(
                "Runtime.evaluate",
                {
                    "expression": (
                        "(()=>{const r=document.querySelector('#progress').getBoundingClientRect();"
                        "return {x:r.x,y:r.y,width:r.width,height:r.height,"
                        "vw:innerWidth,vh:innerHeight};})()"
                    ),
                    "returnByValue": True,
                },
            )
            geometry = geometry_result["result"]["value"]
            start_x = (geometry["x"] + geometry["width"] / 2) / geometry["vw"]
            start_y = (geometry["y"] + geometry["height"] / 2) / geometry["vh"]
            end_x = min(0.99, start_x + 80 / geometry["vw"])
            trajectory = tuple(
                VisualDragPoint(
                    start_x + (end_x - start_x) * index / 10,
                    start_y,
                    10 if index else 0,
                )
                for index in range(11)
            )
            screenshot = await driver.capture_evidence("visual-drag-before")
            screenshot_fingerprint = hashlib.sha256(screenshot.read_bytes()).hexdigest()

            receipt = await driver.execute(
                ActionCommand(
                    "visual-drag-progress",
                    ActionKind.VISUAL_DRAG,
                    visual_trajectory=trajectory,
                    observation_fingerprint=observation.fingerprint,
                    screenshot_fingerprint=screenshot_fingerprint,
                    visual_confidence=0.99,
                    drag_risk=DragRiskClass.BUSINESS,
                    expected=ExpectedCondition("text_contains", "进度已调整", 5),
                )
            )
            verification = await driver.verify(ExpectedCondition("text_contains", "进度已调整", 5))

            assert receipt.success, receipt.message
            assert receipt.data["可视指针反馈"] is True
            assert verification.success, verification.reason
        finally:
            await driver.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_real_visible_chrome_reuses_profile_without_webdriver_signal(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = BrowserConfig(
            headless=False,
            profile_root=tmp_path / "profiles-visible",
            profile_key="stable-account",
            reuse_profile=True,
            command_timeout_seconds=10,
            launch_timeout_seconds=20,
        )
        driver = CdpAutomationDriver(config, tmp_path / "artifacts-visible")
        try:
            await driver.start()

            diagnostics = driver.environment_diagnostics
            assert diagnostics["webdriver"] is False
            assert "HeadlessChrome" not in str(diagnostics["userAgent"])
            assert diagnostics["visibilityState"] == "visible"
            assert driver.context_id is None
        finally:
            await driver.close()

        assert (config.profile_root / config.profile_key).is_dir()

    asyncio.run(scenario())

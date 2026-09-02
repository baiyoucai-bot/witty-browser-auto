"""用真实 Chrome 验证原生对话框不再挂起会话。

回归的是一个真实故障：`Page.enable` 之后不应答 `Page.javascriptDialogOpening`，
渲染进程会一直挂起，点击卡到超时并误报失败，随后所有观察都抛 Runtime.evaluate 超时。
因此这里的判据是"弹窗之后会话还能不能继续用"，而不是"策略字段对不对"。
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

_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>弹窗验收</title></head>
<body>
<h1 id="title">弹窗验收</h1>
<div id="result">idle</div>
<button id="confirm-button">删除</button>
<button id="prompt-button">备注</button>
<button id="alert-button">提示</button>
<script>
const show = (value) => { document.getElementById('result').textContent = String(value); };
const ask = document.getElementById('confirm-button');
ask.onclick = () => show('confirm:' + confirm('确定删除吗?'));
const note = document.getElementById('prompt-button');
note.onclick = () => show('prompt:' + prompt('填写备注', '默认备注'));
document.getElementById('alert-button').onclick = () => { alert('已完成'); show('alert:done'); };
</script>
</body></html>"""


async def _serve() -> tuple[web.AppRunner, str]:
    async def page(_: web.Request) -> web.Response:
        return web.Response(text=_PAGE, content_type="text/html")

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("弹窗验收服务未返回监听端口")
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
            memory_database=tmp_path / "m.db",
            artifact_root=tmp_path / "artifacts",
        ),
    )


def test_dialogs_do_not_freeze_the_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, base_url = await _serve()
        try:
            async with launch_browser_toolkit(
                base_url,
                goal="弹窗验收",
                config=_config(tmp_path),
                allowed_origins=[base_url],
                inputs={"note": "缺货退款"},
            ) as toolkit:

                async def click(button_id: str) -> None:
                    observation = await toolkit.observe(force=True)
                    target = next(
                        candidate
                        for candidate in observation.candidates
                        if button_id.split("-")[0] in (candidate.recipe.value or "")
                    )
                    await toolkit.call(
                        "click",
                        target_id=target.target_id,
                        expect_kind="fingerprint_changed",
                        expect_value=observation.fingerprint,
                    )

                async def result_text() -> str:
                    # 会话没被挂住时，这次观察必须能在超时之前正常返回。
                    observation = await asyncio.wait_for(toolkit.observe(force=True), timeout=15)
                    return observation.summary

                # 默认策略：confirm 取消。页面因此拿到 false。
                await click("confirm-button")
                assert "confirm:false" in await result_text()

                # 一次性改成 accept，只对下一次生效。
                await toolkit.handle_dialog("accept", scope="next")
                await click("confirm-button")
                assert "confirm:true" in await result_text()

                await click("confirm-button")
                assert "confirm:false" in await result_text(), "一次性规则不应影响第二次"

                # prompt 用任务输入键填值，明文不进工具参数。
                await toolkit.handle_dialog(
                    "accept",
                    scope="next",
                    dialog_kinds=["prompt"],
                    prompt_text_input_key="note",
                )
                await click("prompt-button")
                assert "prompt:缺货退款" in await result_text()

                # alert 只有一个按钮，默认确认即可，页面脚本得以继续执行。
                await click("alert-button")
                assert "alert:done" in await result_text()

                inspected = await toolkit.handle_dialog("inspect")
                assert inspected.success
                kinds = [record["kind"] for record in inspected.data["dialogs"]]
                assert kinds == ["confirm", "confirm", "confirm", "prompt", "alert"]
                assert [record["action"] for record in inspected.data["dialogs"]] == [
                    "dismiss",
                    "accept",
                    "dismiss",
                    "accept",
                    "accept",
                ]
                # 填进 prompt 的任务输入值不得进入模型视图。
                assert inspected.data["dialogs"][3]["prompt_text"] == "缺货退款"
                assert inspected.model_data["dialogs"][3]["prompt_text"] == "[REDACTED]"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

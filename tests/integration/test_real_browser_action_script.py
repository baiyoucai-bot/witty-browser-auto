"""用真实 Chrome 验证导出的动作脚本能在独立进程里把流程重跑一遍。

只断言"脚本文本长得对"是没有意义的：脚本的价值全在于能不能真的再跑通。因此这里
先用工具驱动一次登录，导出脚本，再让子进程用另一个 Chrome 执行它，最后看服务端
是否记录到第二次成功登录。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
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

_USERNAME = "zhangsan"
_PASSWORD = "s3cret-pa55"

_LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>登录</title></head>
<body>
<h1>请登录</h1>
<form method="post" action="/login">
  <input data-testid="username-field" name="username" type="text" placeholder="用户名">
  <input id="password-field" name="password" type="password" placeholder="密码">
  <button id="submit-button" type="submit">登录</button>
</form>
</body></html>"""

_HOME_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>控制台</title></head>
<body><h1 id="welcome">欢迎回来</h1></body></html>"""


async def _serve(logins: list[str]) -> tuple[web.AppRunner, str]:
    async def login_page(_: web.Request) -> web.Response:
        return web.Response(text=_LOGIN_PAGE, content_type="text/html")

    async def home_page(_: web.Request) -> web.Response:
        return web.Response(text=_HOME_PAGE, content_type="text/html")

    async def submit(request: web.Request) -> web.Response:
        form = await request.post()
        if form.get("username") == _USERNAME and form.get("password") == _PASSWORD:
            logins.append(str(form.get("username")))
            raise web.HTTPFound("/home")
        return web.Response(text="凭据错误", status=401)

    app = web.Application()
    app.router.add_get("/", login_page)
    app.router.add_post("/login", submit)
    app.router.add_get("/home", home_page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("登录验收服务未返回监听端口")
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}"


def _config(tmp_path: Path, tag: str) -> AppConfig:
    return AppConfig(
        browser=BrowserConfig(
            headless=True,
            profile_root=tmp_path / f"profiles-{tag}",
            command_timeout_seconds=10,
            launch_timeout_seconds=20,
        ),
        storage=StorageConfig(
            memory_database=tmp_path / f"{tag}.db",
            artifact_root=tmp_path / f"artifacts-{tag}",
        ),
    )


def _candidate_id(observation, *, role: str, keyword: str) -> str:
    for candidate in observation.candidates:
        haystack = f"{candidate.name} {candidate.text} {candidate.recipe.value or ''}"
        if candidate.role == role and keyword in haystack:
            return candidate.target_id
    seen = [candidate.name for candidate in observation.candidates]
    raise AssertionError(f"观察里找不到 {role}/{keyword}：{seen}")


def test_exported_script_replays_the_login_flow(tmp_path: Path) -> None:
    logins: list[str] = []
    repository_root = str(Path(__file__).resolve().parents[2])

    async def scenario() -> tuple[str, int]:
        runner, base_url = await _serve(logins)
        try:
            async with launch_browser_toolkit(
                base_url,
                goal="登录后台",
                config=_config(tmp_path, "record"),
                allowed_origins=[base_url],
                inputs={"account": _USERNAME, "secret": _PASSWORD},
            ) as toolkit:
                observation = await toolkit.observe()
                username_id = _candidate_id(observation, role="textbox", keyword="username")
                await toolkit.call("input_text", target_id=username_id, input_key="account")

                observation = await toolkit.observe()
                password_id = _candidate_id(observation, role="textbox", keyword="password")
                await toolkit.call("input_text", target_id=password_id, input_key="secret")

                observation = await toolkit.observe()
                submit_id = _candidate_id(observation, role="button", keyword="登录")
                clicked = await toolkit.call(
                    "click",
                    target_id=submit_id,
                    expect_kind="url_contains",
                    expect_value="/home",
                )
                assert clicked.success, clicked.message

                exported = await toolkit.export_action_script()
                assert exported.success, exported.message

            # 脚本必须在服务仍然在线时重跑，否则验证的就只是它能不能连上而已。
            script = exported.data["code"]
            runnable = script.replace('"account": ""', f'"account": {_USERNAME!r}').replace(
                '"secret": ""', f'"secret": {_PASSWORD!r}'
            )
            script_path = tmp_path / "replay.py"
            script_path.write_text(runnable, encoding="utf-8")

            env = dict(os.environ)
            env.update(
                {
                    "WITTY_BROWSER_AUTO_HEADLESS": "1",
                    "WITTY_BROWSER_AUTO_PROFILE_ROOT": str(tmp_path / "profiles-replay"),
                    "WITTY_BROWSER_AUTO_MEMORY_DB": str(tmp_path / "replay.db"),
                    "WITTY_BROWSER_AUTO_ARTIFACT_ROOT": str(tmp_path / "artifacts-replay"),
                }
            )
            completed = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(script_path)],
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
                cwd=repository_root,
            )
            assert completed.returncode == 0, (
                f"导出的脚本重跑失败\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
            return script, len(logins)
        finally:
            await runner.cleanup()

    script, login_count = asyncio.run(scenario())

    # 录制一次、重跑一次，服务端应当看到两次成功登录。
    assert login_count == 2, f"服务端记录的登录次数不是 2：{login_count}"

    # 脚本必须自带可跨会话的定位器，且不泄露口令或会话内标识。
    assert '"strategy": "test_id"' in script
    assert '"value": "username-field"' in script
    assert '"strategy": "css"' in script
    assert '"value": "#password-field"' in script
    assert _PASSWORD not in script
    assert _USERNAME not in script
    assert "input_key" in script

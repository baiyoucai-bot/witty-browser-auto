"""用真实 Chrome 验证批量填写、独立等待与会话态复用。

会话态那项的判据取"第二个浏览器有没有再登录一次"：服务端统计登录次数，导入快照后
直接访问受保护页面必须成功且登录次数保持不变——真跳过登录才算数，仅仅"导入没报错"
证明不了任何事。
"""

from __future__ import annotations

import asyncio
import json
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

_FORM_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>报名表</title></head>
<body>
<h1>报名表</h1>
<form id="signup">
  <input id="name" name="name" data-testid="field-name" placeholder="姓名">
  <input id="email" name="email" data-testid="field-email" placeholder="邮箱">
  <input id="phone" name="phone" data-testid="field-phone" placeholder="电话">
  <select id="city" name="city" data-testid="field-city">
    <option value="">请选择城市</option>
    <option value="bj">北京</option>
    <option value="sh">上海</option>
  </select>
  <input id="agree" type="checkbox" name="agree" data-testid="field-agree">
  <button type="button" id="submit" data-testid="submit">提交</button>
</form>
<div id="result">待提交</div>
<script>
// 受控组件：直接赋值不派发事件的话，这里的镜像值不会更新。
const mirror = {};
for (const id of ['name', 'email', 'phone', 'city']) {
  document.getElementById(id).addEventListener('input', (e) => { mirror[id] = e.target.value; });
}
document.getElementById('agree').addEventListener('change', (e) => {
  mirror.agree = e.target.checked;
});
document.getElementById('submit').onclick = () => {
  // 延迟出结果，用来验证独立等待。
  setTimeout(() => {
    document.getElementById('result').textContent = '提交成功：' + JSON.stringify(mirror);
  }, 1500);
};
</script>
</body></html>"""

_LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>登录</title></head>
<body><h1>请登录</h1>
<form method="post" action="/login">
  <input name="user" data-testid="user"><input name="password" type="password" data-testid="pw">
  <button type="submit" data-testid="go">登录</button>
</form></body></html>"""

_HOME_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>控制台</title></head>
<body><h1>控制台</h1><div id="who">已登录：{user}</div>
<script>localStorage.setItem('profile', JSON.stringify({{theme: 'dark'}}));</script>
</body></html>"""


def _config(root: Path) -> AppConfig:
    return AppConfig(
        browser=BrowserConfig(
            headless=True,
            profile_root=root / "profiles",
            command_timeout_seconds=15,
            launch_timeout_seconds=20,
        ),
        storage=StorageConfig(
            memory_database=root / "m.db",
            artifact_root=root / "artifacts",
        ),
    )


async def _serve_form() -> tuple[web.AppRunner, str]:
    app = web.Application()

    async def page(_: web.Request) -> web.Response:
        return web.Response(text=_FORM_PAGE, content_type="text/html")

    app.router.add_get("/", page)
    return await _start(app)


async def _start(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    if site._server is None or not site._server.sockets:
        await runner.cleanup()
        raise RuntimeError("验收服务未返回监听端口")
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}/"


def test_fill_form_writes_every_field_in_one_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, base_url = await _serve_form()
        try:
            async with launch_browser_toolkit(
                base_url,
                goal="批量填表",
                config=_config(tmp_path),
                allowed_origins=[base_url],
                inputs={"applicant": "张三"},
            ) as toolkit:
                observation = await toolkit.observe(force=True)
                by_test_id: dict[str, str] = {}
                for candidate in observation.candidates:
                    value = candidate.recipe.value or ""
                    for name in ("name", "email", "phone", "city", "agree"):
                        if f"field-{name}" in value:
                            by_test_id[name] = candidate.target_id
                assert len(by_test_id) == 5, f"未识别齐全部字段：{sorted(by_test_id)}"

                filled = await toolkit.fill_form(
                    [
                        {"target_id": by_test_id["name"], "input_key": "applicant"},
                        {"target_id": by_test_id["email"], "text": "zhang@test.com"},
                        {"target_id": by_test_id["phone"], "text": "13800000000"},
                        # 用可见文本而不是 value 选择，调用方看到的就是"北京"。
                        {"target_id": by_test_id["city"], "select_value": "北京"},
                        {"target_id": by_test_id["agree"], "checked": True},
                    ]
                )
                assert filled.success, filled.message
                assert filled.data["filled_count"] == 5
                # 敏感值不得出现在返回结构里。
                assert "张三" not in json.dumps(filled.data, ensure_ascii=False)

                session = toolkit.driver.session
                state = await session.call(
                    "Runtime.evaluate",
                    {
                        # 必须走 getElementById，裸 name 会拿到 window.name 这个全局字符串。
                        "expression": (
                            "(()=>{const g=(id)=>document.getElementById(id);"
                            "return {name:g('name').value,email:g('email').value,"
                            "phone:g('phone').value,city:g('city').value,"
                            "agree:g('agree').checked};})()"
                        ),
                        "returnByValue": True,
                    },
                )
                actual = state.get("result", {}).get("value")
                assert actual == {
                    "name": "张三",
                    "email": "zhang@test.com",
                    "phone": "13800000000",
                    "city": "bj",
                    "agree": True,
                }, actual

                # 提交后结果延迟 1.5 秒出现，独立等待必须能等到。
                submit = next(
                    candidate.target_id
                    for candidate in (await toolkit.observe(force=True)).candidates
                    if "submit" in (candidate.recipe.value or "")
                )
                await toolkit.call(
                    "click", target_id=submit, expect_kind="text_contains", expect_value="提交成功"
                )
                waited = await toolkit.wait_for_condition(
                    "text_contains", "提交成功", timeout_seconds=15
                )
                assert waited.success, waited.message
                # 受控组件的镜像值只有在事件正确派发时才会有内容。
                summary = (await toolkit.observe(force=True)).summary
                assert "北京" in summary or "bj" in summary
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_wait_for_condition_reports_timeout_without_raising(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, base_url = await _serve_form()
        try:
            async with launch_browser_toolkit(
                base_url,
                goal="等待超时",
                config=_config(tmp_path),
                allowed_origins=[base_url],
            ) as toolkit:
                result = await toolkit.wait_for_condition(
                    "text_contains", "永远不会出现的文本", timeout_seconds=2
                )
                # 等不到是业务结果而不是异常，调用方据此决定下一步。
                assert result.success is False
                assert result.data["satisfied"] is False
                assert result.data["waited_seconds"] >= 1.5
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_storage_state_lets_a_second_browser_skip_login(tmp_path: Path) -> None:
    logins: list[str] = []

    async def scenario() -> None:
        async def login_page(_: web.Request) -> web.Response:
            return web.Response(text=_LOGIN_PAGE, content_type="text/html")

        async def do_login(request: web.Request) -> web.Response:
            form = await request.post()
            user = str(form.get("user", ""))
            logins.append(user)
            response = web.HTTPFound("/home")
            response.set_cookie("session_id", f"token-for-{user}", path="/")
            raise response

        async def home(request: web.Request) -> web.Response:
            cookie = request.cookies.get("session_id", "")
            if not cookie.startswith("token-for-"):
                raise web.HTTPFound("/login")
            user = cookie.removeprefix("token-for-")
            return web.Response(text=_HOME_PAGE.format(user=user), content_type="text/html")

        app = web.Application()
        app.router.add_get("/login", login_page)
        app.router.add_post("/login", do_login)
        app.router.add_get("/home", home)
        app.router.add_get("/", home)
        runner, base_url = await _start(app)

        try:
            # 第一个浏览器：真的登录一次，然后导出会话态。
            async with launch_browser_toolkit(
                f"{base_url}login",
                goal="登录并导出会话态",
                config=_config(tmp_path / "first"),
                allowed_origins=[base_url],
                inputs={"user": "alice", "password": "s3cret"},
            ) as toolkit:
                observation = await toolkit.observe(force=True)
                fields = {}
                for candidate in observation.candidates:
                    value = candidate.recipe.value or ""
                    for name in ("user", "pw", "go"):
                        if name in value:
                            fields[name] = candidate.target_id
                await toolkit.fill_form(
                    [
                        {"target_id": fields["user"], "input_key": "user"},
                        {"target_id": fields["pw"], "input_key": "password"},
                    ]
                )
                await toolkit.call(
                    "click",
                    target_id=fields["go"],
                    expect_kind="url_contains",
                    expect_value="/home",
                )
                assert logins == ["alice"], logins

                exported = await toolkit.manage_storage_state("export")
                assert exported.success, exported.message
                state_path = exported.data["file_path"]
                assert exported.data["summary"]["cookie_count"] >= 1
                # 会话凭据不能进模型上下文。
                assert "token-for-alice" not in json.dumps(exported.model_data, ensure_ascii=False)

            # 第二个浏览器：完全独立的 profile，导入快照后直接进受保护页。
            async with launch_browser_toolkit(
                f"{base_url}login",
                goal="复用会话态",
                config=_config(tmp_path / "second"),
                allowed_origins=[base_url],
            ) as toolkit:
                imported = await toolkit.manage_storage_state("import", file_path=state_path)
                assert imported.success, imported.message
                assert imported.data["cookies_applied"] >= 1

                await toolkit.navigate(f"{base_url}home")
                summary = (await toolkit.observe(force=True)).summary
                assert "已登录：alice" in summary, summary

            # 判据：第二个浏览器一次都没有再登录。
            assert logins == ["alice"], f"会话态没有真正复用，登录记录：{logins}"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

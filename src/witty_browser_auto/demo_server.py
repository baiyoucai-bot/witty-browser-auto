"""用于原生 CDP 验收的本地确定性网页。"""

from __future__ import annotations

import argparse

from aiohttp import web

HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Witty 浏览器工具库 本地验收</title></head>
<body>
  <main>
    <h1>任务面板</h1>
    <label for="name">名称</label>
    <input id="name" name="name" autocomplete="off">
    <button id="submit" type="button">提交</button>
    <p id="result" aria-live="polite">等待提交</p>
    <label for="progress">进度</label>
    <input id="progress" name="progress" type="range" min="0" max="100" value="50">
    <p id="progress-result" aria-live="polite">进度未调整</p>
  </main>
  <script>
    document.querySelector('#submit').addEventListener('click', async () => {
      const name = document.querySelector('#name').value;
      const response = await fetch(
        '/api/submit?token=demo-secret&name=' + encodeURIComponent(name)
      );
      const data = await response.json();
      document.querySelector('#result').textContent = '完成：' + data.name;
    });
    document.querySelector('#progress').addEventListener('input', (event) => {
      document.querySelector('#progress-result').textContent =
        '进度已调整：' + event.target.value;
    });
  </script>
</body>
</html>"""


async def index(request: web.Request) -> web.Response:
    return web.Response(text=HTML, content_type="text/html")


async def submit(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "name": request.query.get("name", "")})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/submit", submit)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="启动Witty 浏览器工具库 本地验收网页")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    web.run_app(create_app(), host=args.host, port=args.port)

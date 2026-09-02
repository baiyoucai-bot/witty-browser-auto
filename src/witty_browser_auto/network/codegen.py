"""把捕获的网络交换导出成可独立运行的调用代码。

抓包工具的"复制为 cURL"只解决了一半问题：真正要把接口接进代码，还得知道哪些
Header 是凭据、正文该用哪种编码方式发。这里统一处理这两件事——凭据默认收敛成
环境变量占位，正文按 Content-Type 选择目标语言里正确的传参方式。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl

from witty_browser_auto.security.redaction import is_sensitive_key

CODE_TARGETS: tuple[str, ...] = (
    "curl",
    "python_requests",
    "python_httpx",
    "javascript_fetch",
    "node_axios",
)

# HTTP/2 伪 Header 不能出现在客户端代码里；长度与编码由各语言的 HTTP 库自己算，
# 照抄浏览器的值只会让请求发不出去或被服务端判定为畸形。
_DROPPED_HEADERS = frozenset(
    {
        "content-length",
        "host",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "te",
        "trailer",
        "accept-encoding",
    }
)
_ENV_NAME = re.compile(r"[^A-Z0-9]+")
_MAX_BODY_CHARS = 200_000


class CodeGenerationError(ValueError):
    """交换不具备生成代码的条件。"""


def _env_name(header: str) -> str:
    return _ENV_NAME.sub("_", header.upper()).strip("_") or "SECRET"


def _is_secret_header(name: str) -> bool:
    lowered = name.lower()
    if is_sensitive_key(lowered):
        return True
    return lowered in {"x-csrf-token", "x-xsrf-token", "x-auth-token", "proxy-authorization"}


def _select_headers(
    headers: Mapping[str, str],
    *,
    include_secrets: bool,
) -> tuple[list[tuple[str, str, str]], list[dict[str, str]]]:
    """返回 (名称, 明文值, 占位环境变量名) 与占位说明；占位名为空表示直接用明文。"""

    selected: list[tuple[str, str, str]] = []
    placeholders: list[dict[str, str]] = []
    for name in sorted(headers):
        if name.startswith(":") or name.lower() in _DROPPED_HEADERS:
            continue
        value = headers[name]
        if not include_secrets and _is_secret_header(name):
            variable = _env_name(name)
            selected.append((name, value, variable))
            placeholders.append({"header": name, "env": variable})
            continue
        selected.append((name, value, ""))
    return selected, placeholders


def _content_kind(headers: Mapping[str, str], body: str | None) -> str:
    if body is None:
        return "none"
    content_type = ""
    for name, value in headers.items():
        if name.lower() == "content-type":
            content_type = value.lower()
            break
    if "json" in content_type:
        return "json"
    if "x-www-form-urlencoded" in content_type:
        return "form"
    if "multipart/form-data" in content_type:
        return "multipart"
    return "raw"


def _parsed_json(body: str) -> Any:
    stripped = body.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _python_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_request_code(
    *,
    target: str,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: str | None,
    body_is_binary: bool = False,
    include_secrets: bool = False,
) -> dict[str, Any]:
    """生成一段可独立运行的请求代码。"""

    if target not in CODE_TARGETS:
        raise CodeGenerationError(f"不支持的代码目标：{target}")
    if not url:
        raise CodeGenerationError("交换缺少可用的请求地址")
    if body_is_binary:
        body = None
    if body is not None and len(body) > _MAX_BODY_CHARS:
        raise CodeGenerationError(f"请求正文超过 {_MAX_BODY_CHARS} 字符，无法内联进代码")

    verb = method.upper() or "GET"
    selected, placeholders = _select_headers(headers, include_secrets=include_secrets)
    kind = _content_kind(headers, body)
    builder = {
        "curl": _curl,
        "python_requests": _python_requests,
        "python_httpx": _python_httpx,
        "javascript_fetch": _javascript_fetch,
        "node_axios": _node_axios,
    }[target]
    code = builder(verb, url, selected, body, kind)
    return {
        "target": target,
        "language": "shell" if target == "curl" else _language_of(target),
        "code": code,
        "placeholders": placeholders,
        "body_kind": kind,
        "body_omitted_binary": body_is_binary,
        "include_secrets": include_secrets,
    }


def _language_of(target: str) -> str:
    return "python" if target.startswith("python") else "javascript"


# ----------------------------------------------------------------------
# curl
# ----------------------------------------------------------------------


def _curl(
    method: str,
    url: str,
    headers: list[tuple[str, str, str]],
    body: str | None,
    kind: str,
) -> str:
    lines = [f"curl -X {method} {_shell_quote(url)}"]
    for name, value, variable in headers:
        if variable:
            lines.append(f'  -H "{name}: ${variable}"')
        else:
            lines.append(f"  -H {_shell_quote(f'{name}: {value}')}")
    if body is not None:
        lines.append(f"  --data-raw {_shell_quote(body)}")
    return " \\\n".join(lines)


# ----------------------------------------------------------------------
# Python
# ----------------------------------------------------------------------


def _python_headers(headers: list[tuple[str, str, str]]) -> tuple[str, bool]:
    if not headers:
        return "{}", False
    uses_env = False
    rows = []
    for name, value, variable in headers:
        if variable:
            uses_env = True
            rows.append(f"    {_python_literal(name)}: os.environ[{_python_literal(variable)}],")
        else:
            rows.append(f"    {_python_literal(name)}: {_python_literal(value)},")
    return "{\n" + "\n".join(rows) + "\n}", uses_env


def _python_body(body: str | None, kind: str) -> tuple[str, str]:
    """返回 (赋值语句块, 传给请求函数的关键字参数)。"""

    if body is None:
        return "", ""
    if kind == "json":
        parsed = _parsed_json(body)
        if parsed is not None:
            rendered = json.dumps(parsed, ensure_ascii=False, indent=4)
            return f"payload = {rendered}\n", "json=payload"
    if kind == "form":
        pairs = parse_qsl(body, keep_blank_values=True)
        if pairs:
            rows = "\n".join(
                f"    {_python_literal(name)}: {_python_literal(value)}," for name, value in pairs
            )
            return "payload = {\n" + rows + "\n}\n", "data=payload"
    return f"payload = {_python_literal(body)}\n", "data=payload"


def _python_requests(
    method: str,
    url: str,
    headers: list[tuple[str, str, str]],
    body: str | None,
    kind: str,
) -> str:
    header_block, uses_env = _python_headers(headers)
    body_block, body_kwarg = _python_body(body, kind)
    imports = ["import requests"]
    if uses_env:
        imports.insert(0, "import os")
    call_args = ["url", "headers=headers"]
    if body_kwarg:
        call_args.append(body_kwarg)
    call_args.append("timeout=30")
    return (
        "\n".join(imports)
        + "\n\n"
        + f"url = {_python_literal(url)}\n"
        + f"headers = {header_block}\n"
        + body_block
        + "\n"
        + f"response = requests.request({_python_literal(method)}, "
        + ", ".join(call_args)
        + ")\n"
        + "response.raise_for_status()\n"
        + 'print(response.json() if response.headers.get("content-type", "")'
        + '.startswith("application/json") else response.text)\n'
    )


def _python_httpx(
    method: str,
    url: str,
    headers: list[tuple[str, str, str]],
    body: str | None,
    kind: str,
) -> str:
    header_block, uses_env = _python_headers(headers)
    body_block, body_kwarg = _python_body(body, kind)
    imports = ["import httpx"]
    if uses_env:
        imports.insert(0, "import os")
    call_args = [_python_literal(method), "url", "headers=headers"]
    if body_kwarg:
        call_args.append(body_kwarg)
    return (
        "\n".join(imports)
        + "\n\n"
        + f"url = {_python_literal(url)}\n"
        + f"headers = {header_block}\n"
        + body_block
        + "\n"
        + "with httpx.Client(timeout=30) as client:\n"
        + "    response = client.request("
        + ", ".join(call_args)
        + ")\n"
        + "    response.raise_for_status()\n"
        + "    print(response.text)\n"
    )


# ----------------------------------------------------------------------
# JavaScript
# ----------------------------------------------------------------------


def _javascript_headers(headers: list[tuple[str, str, str]]) -> str:
    if not headers:
        return "{}"
    rows = []
    for name, value, variable in headers:
        if variable:
            rows.append(f"    {json.dumps(name)}: process.env.{variable},")
        else:
            rows.append(f"    {json.dumps(name)}: {json.dumps(value, ensure_ascii=False)},")
    return "{\n" + "\n".join(rows) + "\n  }"


def _reindent(text: str, prefix: str) -> str:
    """让内联的多行 JSON 跟随宿主语句缩进；首行由调用处定位。"""

    lines = text.splitlines()
    if len(lines) <= 1:
        return text
    return "\n".join([lines[0], *(f"{prefix}{line}" for line in lines[1:])])


def _javascript_body(body: str | None, kind: str) -> str:
    if body is None:
        return ""
    if kind == "json":
        parsed = _parsed_json(body)
        if parsed is not None:
            rendered = _reindent(json.dumps(parsed, ensure_ascii=False, indent=2), "  ")
            return f"JSON.stringify({rendered})"
    return json.dumps(body, ensure_ascii=False)


def _javascript_fetch(
    method: str,
    url: str,
    headers: list[tuple[str, str, str]],
    body: str | None,
    kind: str,
) -> str:
    parts = [
        f"const response = await fetch({json.dumps(url)}, {{",
        f"  method: {json.dumps(method)},",
        f"  headers: {_javascript_headers(headers)},",
    ]
    rendered = _javascript_body(body, kind)
    if rendered:
        parts.append(f"  body: {rendered},")
    parts.append("});")
    parts.append("if (!response.ok) throw new Error(`HTTP ${response.status}`);")
    parts.append("console.log(await response.text());")
    return "\n".join(parts)


def _node_axios(
    method: str,
    url: str,
    headers: list[tuple[str, str, str]],
    body: str | None,
    kind: str,
) -> str:
    parts = [
        'import axios from "axios";',
        "",
        "const response = await axios({",
        f"  method: {json.dumps(method.lower())},",
        f"  url: {json.dumps(url)},",
        f"  headers: {_javascript_headers(headers)},",
    ]
    if body is not None:
        parsed = _parsed_json(body) if kind == "json" else None
        if parsed is not None:
            rendered = _reindent(json.dumps(parsed, ensure_ascii=False, indent=2), "  ")
            parts.append(f"  data: {rendered},")
        else:
            parts.append(f"  data: {json.dumps(body, ensure_ascii=False)},")
    parts.append("  timeout: 30000,")
    parts.append("});")
    parts.append("console.log(response.data);")
    return "\n".join(parts)

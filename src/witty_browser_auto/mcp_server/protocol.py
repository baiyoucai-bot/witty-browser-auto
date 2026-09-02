"""MCP stdio 传输的 JSON-RPC 帧层。

MCP 的 stdio 传输是换行分隔的 JSON，不是 LSP 那种 Content-Length 分帧；这里只用标准库
实现，与项目"运行依赖只有 aiohttp"的约束一致。

帧层不认识浏览器，也不认识工具：它只负责把一行文本变成请求、把结果变成一行文本，
这样服务端逻辑可以在测试里直接喂字符串驱动，不必真的接管进程的 stdin/stdout。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

JSONRPC_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSONRPC_VERSION",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "JsonRpcError",
    "JsonRpcRequest",
    "encode_message",
    "error_response",
    "parse_request",
    "success_response",
]


class JsonRpcError(Exception):
    """协议层错误；带 JSON-RPC 错误码，由服务端转成 error 响应。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    method: str
    params: dict[str, Any]
    request_id: Any = None
    # JSON-RPC 用"有没有 id"区分请求与通知，而 id 本身允许是 null，两者不能混为一谈。
    has_id: bool = False

    @property
    def is_notification(self) -> bool:
        return not self.has_id


def parse_request(line: str) -> JsonRpcRequest:
    """把一行文本解析成请求；格式不合法时抛 JsonRpcError。"""

    try:
        payload = json.loads(line)
    except ValueError as exc:
        raise JsonRpcError(PARSE_ERROR, f"不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise JsonRpcError(INVALID_REQUEST, "JSON-RPC 消息必须是对象")
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        raise JsonRpcError(INVALID_REQUEST, "缺少 method 字段")
    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise JsonRpcError(INVALID_PARAMS, "params 必须是对象")
    return JsonRpcRequest(
        method=method,
        params=params,
        request_id=payload.get("id"),
        has_id="id" in payload,
    )


def success_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def encode_message(message: dict[str, Any]) -> str:
    """序列化成单行 JSON；正文里的换行会被转义，不会破坏分帧。"""

    return json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"

"""MCP stdio 客户端、项目级配置和模型工具适配。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PROTOCOL_VERSION = "2025-06-18"
_SERVER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_TOOL_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_ARGUMENTS = 32
_MAX_TOOL_OUTPUT_CHARACTERS = 32_000
_MAX_DISCOVERED_TOOLS = 64
_MAX_RUNTIME_TOOLS = 4


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    server_id: str
    name: str
    command: str
    args: tuple[str, ...] = ()
    enabled: bool = True
    allowed_tools: tuple[str, ...] = ()
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not _SERVER_ID_PATTERN.fullmatch(self.server_id):
            raise ValueError("MCP 标识只能包含字母、数字、下划线和短横线")
        if not self.name.strip() or len(self.name) > 80:
            raise ValueError("MCP 名称必须是 1 到 80 个字符")
        if not self.command.strip() or "\x00" in self.command or len(self.command) > 1024:
            raise ValueError("MCP 启动命令格式无效")
        if len(self.args) > _MAX_ARGUMENTS or any(
            not isinstance(argument, str) or "\x00" in argument or len(argument) > 2048
            for argument in self.args
        ):
            raise ValueError("MCP 启动参数数量或长度超出限制")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("MCP 请求超时必须在 1 到 120 秒之间")
        if len(self.allowed_tools) > _MAX_DISCOVERED_TOOLS:
            raise ValueError("MCP 允许工具数量超出限制")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> McpServerConfig:
        args = value.get("args", ())
        allowed_tools = value.get("allowed_tools", ())
        if not isinstance(args, list | tuple) or not all(isinstance(item, str) for item in args):
            raise ValueError("MCP 启动参数必须是文本数组")
        if not isinstance(allowed_tools, list | tuple) or not all(
            isinstance(item, str) for item in allowed_tools
        ):
            raise ValueError("MCP 允许工具必须是文本数组")
        timeout = value.get("timeout_seconds", 20.0)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float):
            raise ValueError("MCP 请求超时必须是数字")
        return cls(
            server_id=str(value.get("server_id", "")),
            name=str(value.get("name", "")),
            command=str(value.get("command", "")),
            args=tuple(args),
            enabled=value.get("enabled", True) is True,
            allowed_tools=tuple(dict.fromkeys(allowed_tools)),
            timeout_seconds=float(timeout),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "name": self.name,
            "command": self.command,
            "args": list(self.args),
            "enabled": self.enabled,
            "allowed_tools": list(self.allowed_tools),
            "timeout_seconds": self.timeout_seconds,
        }


class ProjectMcpRegistry:
    """把 MCP 配置原子写入项目私有目录。"""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.path = self.project_root / ".witty-browser-auto" / "mcp-servers.json"

    def list(self) -> tuple[McpServerConfig, ...]:
        if not self.path.is_file():
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("MCP 配置文件损坏，需在设置中修复") from exc
        servers = payload.get("servers", []) if isinstance(payload, dict) else []
        if not isinstance(servers, list):
            raise ValueError("MCP 配置中的 servers 必须是数组")
        return tuple(McpServerConfig.from_dict(item) for item in servers if isinstance(item, dict))

    def get(self, server_id: str) -> McpServerConfig | None:
        return next((item for item in self.list() if item.server_id == server_id), None)

    def save(self, config: McpServerConfig) -> McpServerConfig:
        servers = [item for item in self.list() if item.server_id != config.server_id]
        servers.append(config)
        self._write(tuple(servers))
        return config

    def delete(self, server_id: str) -> None:
        servers = list(self.list())
        filtered = tuple(item for item in servers if item.server_id != server_id)
        if len(filtered) == len(servers):
            raise KeyError(server_id)
        self._write(filtered)

    def _write(self, servers: tuple[McpServerConfig, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {"version": 1, "servers": [item.to_dict() for item in servers]},
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)


class McpStdioClient:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._process is not None:
            return
        self._process = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "witty-browser-auto", "version": "0.1.0"},
            },
        )
        await self._notify("notifications/initialized", {})

    async def list_tools(self) -> tuple[dict[str, Any], ...]:
        result = await self._request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if not isinstance(tools, list):
            raise RuntimeError("MCP tools/list 响应格式无效")
        return tuple(item for item in tools[:_MAX_DISCOVERED_TOOLS] if isinstance(item, dict))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise RuntimeError("MCP tools/call 响应格式无效")
        return result

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        process = self._require_process()
        assert process.stdin is not None
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        await process.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            process = self._require_process()
            assert process.stdin is not None and process.stdout is not None
            self._request_id += 1
            request_id = self._request_id
            message = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
            await process.stdin.drain()
            async with asyncio.timeout(self.config.timeout_seconds):
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        raise RuntimeError("MCP 服务在响应前退出")
                    try:
                        response = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError("MCP 服务返回了无效 JSON-RPC 消息") from exc
                    if not isinstance(response, dict) or response.get("id") != request_id:
                        continue
                    if "error" in response:
                        error = response.get("error")
                        message_text = error.get("message") if isinstance(error, dict) else error
                        raise RuntimeError(f"MCP 调用失败：{message_text}")
                    result = response.get("result", {})
                    return result if isinstance(result, dict) else {"value": result}

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None or self._process.returncode is not None:
            raise RuntimeError("MCP 服务尚未启动或已经退出")
        return self._process

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.kill()
                await process.wait()


@dataclass(frozen=True, slots=True)
class McpTool:
    exposed_name: str
    server_id: str
    original_name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool


@dataclass(slots=True)
class McpManager:
    registry: ProjectMcpRegistry
    clients: dict[str, McpStdioClient] = field(default_factory=dict)
    tools: dict[str, McpTool] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    async def initialize(self) -> None:
        enabled = [config for config in self.registry.list() if config.enabled]
        await asyncio.gather(*(self._start_server(config) for config in enabled))

    async def _start_server(self, config: McpServerConfig) -> None:
        client = McpStdioClient(config)
        try:
            async with asyncio.timeout(min(config.timeout_seconds, 5.0)):
                await client.start()
                discovered = await client.list_tools()
            self.clients[config.server_id] = client
            for raw_tool in discovered:
                tool = _adapt_tool(config, raw_tool)
                if tool is not None:
                    self.tools[tool.exposed_name] = tool
        except Exception as exc:
            self.errors[config.server_id] = str(exc)[:300]
            await client.close()

    def schemas_for_context(self, context: str) -> list[dict[str, Any]]:
        ranked = sorted(
            self.tools.values(),
            key=lambda item: _tool_rank(item, context),
            reverse=True,
        )
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.exposed_name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in ranked[:_MAX_RUNTIME_TOOLS]
        ]

    def handles(self, name: str) -> bool:
        return name in self.tools

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[bool, str, dict[str, Any], bool]:
        tool = self.tools.get(name)
        if tool is None:
            raise ValueError("MCP 工具未发现或不属于当前项目")
        client = self.clients.get(tool.server_id)
        if client is None:
            raise RuntimeError("MCP 服务当前不可用")
        result = await client.call_tool(tool.original_name, arguments)
        is_error = result.get("isError") is True
        safe_result = _bounded_result(result)
        message = f"MCP 工具 {tool.original_name}{'执行失败' if is_error else '执行完成'}"
        return not is_error, message, safe_result, tool.read_only

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for client in self.clients.values()))
        self.clients.clear()


async def diagnose_mcp_server(config: McpServerConfig) -> dict[str, Any]:
    client = McpStdioClient(config)
    started = time.perf_counter()
    try:
        await client.start()
        tools = await client.list_tools()
        return {
            "status": "passed",
            "message": f"连接成功，发现 {len(tools)} 个工具",
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "tools": [
                {
                    "name": str(tool.get("name", ""))[:120],
                    "description": str(tool.get("description", ""))[:240],
                }
                for tool in tools
                if tool.get("name")
            ],
        }
    finally:
        await client.close()


def _adapt_tool(config: McpServerConfig, raw_tool: dict[str, Any]) -> McpTool | None:
    name = raw_tool.get("name")
    if not isinstance(name, str) or not name or len(name) > 128:
        return None
    if config.allowed_tools and name not in config.allowed_tools:
        return None
    normalized_server = _TOOL_NAME_PATTERN.sub("_", config.server_id)[:20]
    normalized_name = _TOOL_NAME_PATTERN.sub("_", name)[:36]
    exposed_name = f"mcp__{normalized_server}__{normalized_name}"[:64]
    schema = raw_tool.get("inputSchema", {"type": "object"})
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
    annotations = raw_tool.get("annotations")
    read_only = isinstance(annotations, dict) and annotations.get("readOnlyHint") is True
    return McpTool(
        exposed_name=exposed_name,
        server_id=config.server_id,
        original_name=name,
        description=f"[MCP: {config.name}] {str(raw_tool.get('description', name))[:600]}",
        input_schema=schema,
        read_only=read_only,
    )


def _tool_rank(tool: McpTool, context: str) -> tuple[int, str]:
    normalized_context = context.casefold()
    source = f"{tool.original_name} {tool.description}".casefold()
    terms = re.findall(r"[A-Za-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}", source)
    matches = sum(1 for term in terms if term in normalized_context)
    return matches, tool.exposed_name


def _bounded_result(result: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    if len(serialized) <= _MAX_TOOL_OUTPUT_CHARACTERS:
        return result
    return {
        "isError": result.get("isError") is True,
        "content": serialized[:_MAX_TOOL_OUTPUT_CHARACTERS],
        "truncated": True,
    }

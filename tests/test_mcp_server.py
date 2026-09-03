"""MCP 服务端的协议、工具映射与错误语义回归。

判据取"客户端实际收到什么"：协议层问题必须回 JSON-RPC error，工具执行失败必须回
`isError` 的正常响应，否则模型读不到原因、连接还被打断；敏感值与本机路径不得出现在
返回内容里。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionReceipt,
    BoundingBox,
    CandidateTarget,
    DriverCapabilities,
    ExecutionScope,
    ExpectedCondition,
    LocatorRecipe,
    Observation,
    TaskSpec,
    VerificationResult,
)
from witty_browser_auto.mcp_server import (
    CORE_TOOL_NAMES,
    PROTOCOL_VERSION,
    SESSION_TOOL_NAMES,
    McpServer,
    ToolkitSession,
    mcp_descriptor,
    profile_definitions,
)
from witty_browser_auto.mcp_server.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    encode_message,
    parse_request,
)
from witty_browser_auto.mcp_server.tools import CLOSE_BROWSER_TOOL, OBSERVE_TOOL, OPEN_BROWSER_TOOL
from witty_browser_auto.toolkit.catalog import BROWSER_TOOLS
from witty_browser_auto.toolkit.facade import BrowserToolkit


def _rpc(server: McpServer, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    response = asyncio.run(server.handle_line(line))
    assert response is not None
    return response


def _call(server: McpServer, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return _rpc(server, "tools/call", {"name": name, "arguments": arguments or {}})["result"]


def _server(**kwargs: Any) -> McpServer:
    return McpServer(session=ToolkitSession(), **kwargs)


# ----------------------------------------------------------------------
# 帧层
# ----------------------------------------------------------------------


def test_request_parsing_distinguishes_notifications() -> None:
    request = parse_request(json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 7}))
    assert request.has_id is True and request.is_notification is False

    notification = parse_request(json.dumps({"jsonrpc": "2.0", "method": "ping"}))
    assert notification.is_notification is True
    # id 显式为 null 仍然是请求，不能当成通知丢掉响应。
    explicit_null = parse_request(json.dumps({"jsonrpc": "2.0", "method": "ping", "id": None}))
    assert explicit_null.is_notification is False


def test_encoded_message_stays_on_one_line() -> None:
    payload = encode_message({"result": {"text": "第一行\n第二行"}})
    assert payload.endswith("\n")
    # 正文里的换行必须被转义，否则会破坏换行分帧。
    assert payload.count("\n") == 1


# ----------------------------------------------------------------------
# 协议握手与清单
# ----------------------------------------------------------------------


def test_initialize_reports_protocol_and_instructions() -> None:
    result = _rpc(_server(), "initialize", {"protocolVersion": PROTOCOL_VERSION})["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert "open_browser" in result["instructions"]
    assert "target_id" in result["instructions"]


def test_notifications_get_no_response() -> None:
    server = _server()
    line = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert asyncio.run(server.handle_line(line)) is None
    # 未知通知也必须被忽略，不能因此打断连接。
    unknown = json.dumps({"jsonrpc": "2.0", "method": "notifications/whatever"})
    assert asyncio.run(server.handle_line(unknown)) is None


def test_core_profile_exposes_session_tools_first() -> None:
    tools = _rpc(_server(profile="core"), "tools/list")["result"]["tools"]
    names = [item["name"] for item in tools]

    assert names[:3] == list(SESSION_TOOL_NAMES)
    assert len(names) == len(CORE_TOOL_NAMES) + len(SESSION_TOOL_NAMES)
    # 每个工具都要给 MCP 平铺的 inputSchema，而不是 OpenAI 的嵌套 function。
    assert all("inputSchema" in item and "function" not in item for item in tools)


def test_all_profile_exposes_every_externally_callable_tool() -> None:
    tools = _rpc(_server(profile="all"), "tools/list")["result"]["tools"]
    assert len(tools) == len(BROWSER_TOOLS.externally_callable()) + len(SESSION_TOOL_NAMES)
    names = {item["name"] for item in tools}
    # 终态工具永远不暴露：客户端调用只会换来一次被拒绝的回合。
    assert not names & {"finish", "ask_user", "block", "wait_until"}


def test_core_tool_names_are_all_real_and_callable() -> None:
    """档位是第二份清单，必须与注册表对齐，否则会静默暴露不存在的工具。"""

    available = {item.name for item in BROWSER_TOOLS.externally_callable()}
    assert set(CORE_TOOL_NAMES) <= available
    assert len(set(CORE_TOOL_NAMES)) == len(CORE_TOOL_NAMES)


def test_category_filter_and_extra_tools() -> None:
    network = profile_definitions("core", categories=("network",))
    assert network and all(item.category == "network" for item in network)

    extended = profile_definitions("core", extra_tools=("save_pdf",))
    assert "save_pdf" in {item.name for item in extended}

    with pytest.raises(ValueError, match="未知的工具档位"):
        profile_definitions("everything")
    with pytest.raises(ValueError, match="不存在或不开放"):
        profile_definitions("core", extra_tools=("finish",))


def test_descriptor_carries_the_return_contract() -> None:
    descriptor = mcp_descriptor(BROWSER_TOOLS.get("navigate"))
    assert descriptor["name"] == "navigate"
    assert "返回：" in descriptor["description"]
    assert descriptor["inputSchema"]["properties"]["url"]["type"] == "string"


def test_descriptor_annotations_follow_registry_flags() -> None:
    """annotations 是客户端决定要不要请求授权的依据，必须与注册表的读写声明一致。"""

    for definition in BROWSER_TOOLS.externally_callable():
        hints = mcp_descriptor(definition)["annotations"]
        write = definition.requires_write_permission
        assert hints["idempotentHint"] is bool(definition.idempotent), definition.name
        assert hints["openWorldHint"] is True
        # 需要写权限的一定不是只读；只读的一定不需要写权限且不改页面（瞬时动作除外）。
        if write:
            assert hints["readOnlyHint"] is False, definition.name
            assert hints["destructiveHint"] is (not definition.idempotent), definition.name
        else:
            assert hints["destructiveHint"] is False, definition.name
            if definition.counts_as_action and definition.name not in {
                "wait",
                "screenshot",
                "scroll",
                "hover",
            }:
                assert hints["readOnlyHint"] is False, definition.name
            else:
                assert hints["readOnlyHint"] is True, definition.name


def test_annotations_do_not_let_clients_auto_approve_page_mutations() -> None:
    """readOnlyHint 的规范含义是"不改变环境"：导航、标签页、网络路由、整页采集都改了浏览器，
    不能因为 read_only 门控放行它们就标成只读，否则客户端会不问用户直接放模型去导航。"""

    def hints(name: str) -> dict[str, bool]:
        return mcp_descriptor(BROWSER_TOOLS.get(name))["annotations"]

    for name in (
        "navigate",
        "navigate_history",
        "open_tab",
        "switch_tab",
        "close_tab",
        "manage_network_route",
        "run_structured_extraction",
        "replay_collection_program",
    ):
        assert hints(name)["readOnlyHint"] is False, name
        # 改了环境但不是破坏性更新：客户端应当询问，而不是按最高级别告警。
        assert hints(name)["destructiveHint"] is False, name
    for name in ("read_page_markdown", "read_element", "inspect_network_traffic", "list_tabs"):
        assert hints(name)["readOnlyHint"] is True, name
    # 瞬时动作：改的是视口与指针，不导航不提交，视为只读。
    for name in ("scroll", "hover", "wait", "screenshot"):
        assert hints(name)["readOnlyHint"] is True, name
    # 业务写：点击非幂等即破坏性；填表幂等则不是。
    click = hints("click")
    assert click["readOnlyHint"] is False and click["destructiveHint"] is True
    fill = hints("fill_form")
    assert fill["readOnlyHint"] is False and fill["destructiveHint"] is False


def test_session_tools_carry_annotations_too() -> None:
    tools = _rpc(_server(profile="core"), "tools/list")["result"]["tools"]
    by_name = {item["name"]: item for item in tools}
    assert all("annotations" in item for item in tools)
    observe = by_name[OBSERVE_TOOL]["annotations"]
    assert observe["readOnlyHint"] is True
    # 观察读的是外部网页，与 read_element 一样属于开放世界。
    assert observe["openWorldHint"] is True
    assert by_name[OPEN_BROWSER_TOOL]["annotations"]["readOnlyHint"] is False
    assert by_name[OPEN_BROWSER_TOOL]["annotations"]["openWorldHint"] is True
    assert by_name[CLOSE_BROWSER_TOOL]["annotations"]["idempotentHint"] is True


# ----------------------------------------------------------------------
# 错误语义
# ----------------------------------------------------------------------


def test_protocol_level_problems_return_jsonrpc_errors() -> None:
    server = _server()
    assert _rpc(server, "does/not/exist")["error"]["code"] == METHOD_NOT_FOUND
    broken = asyncio.run(server.handle_line("{ 不是 json"))
    assert broken is not None and broken["error"]["code"] == PARSE_ERROR
    assert _rpc(server, "tools/call", {})["error"]["code"] == INVALID_PARAMS


def test_tool_failures_return_is_error_instead_of_breaking_the_connection() -> None:
    server = _server()

    no_session = _call(server, "click", {"target_id": "t-1"})
    assert no_session["isError"] is True
    assert "open_browser" in no_session["content"][0]["text"]

    hidden = _call(server, "save_pdf")
    assert hidden["isError"] is True
    text = hidden["content"][0]["text"]
    assert "未在本服务端开放" in text
    # 未开放不是参数问题，不该被贴上"参数无效"的标签。
    assert "参数无效" not in text

    assert _call(server, "close_browser")["content"][0]["text"]


def test_observe_rejects_unknown_and_out_of_range_arguments() -> None:
    server = _server()
    for arguments, expected in (
        ({"max_candidates": 0}, "max_candidates"),
        ({"max_candidates": "3"}, "max_candidates"),
        ({"roles": "button"}, "roles"),
        ({"as_text": "yes"}, "as_text"),
        ({"bogus": 1}, "未知参数"),
    ):
        result = _call(server, "observe", arguments)
        assert result["isError"] is True, arguments
        assert expected in result["content"][0]["text"], arguments


# ----------------------------------------------------------------------
# 带会话的端到端
# ----------------------------------------------------------------------


class _StubDriver:
    """最小驱动：只支持观察与一次点击，避免测试真的启动 Chrome。"""

    capabilities = DriverCapabilities(dom=True, accessibility=True, javascript=True)

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root
        self.url = "https://shop.test/login"
        self.commands: list[ActionCommand] = []
        self.closed = False

    async def start(self) -> None:
        return None

    async def open(self, url: str) -> str:
        self.url = url
        return "surface-1"

    async def observe(self, *, force: bool = False) -> Observation:
        return Observation(
            surface_id="surface-1",
            url=self.url,
            title="登录",
            version=1,
            fingerprint="fp-1",
            summary="页面标题：登录",
            candidates=(
                CandidateTarget(
                    "t-user",
                    "textbox",
                    "用户名",
                    "",
                    0.95,
                    ("测试",),
                    LocatorRecipe("css", value="#user"),
                    BoundingBox(0, 0, 10, 10),
                ),
            ),
        )

    async def execute(self, command: ActionCommand) -> ActionReceipt:
        self.commands.append(command)
        return ActionReceipt(command.action_id, True, True, "已执行", 1.0)

    async def verify(self, condition: ExpectedCondition) -> VerificationResult:
        return VerificationResult(True, "已满足")

    async def capture_evidence(self, label: str) -> Path:
        path = self.artifact_root / f"{label}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return path

    async def close(self) -> None:
        self.closed = True


class _StubSession(ToolkitSession):
    """把装配换成 stub 驱动，其余生命周期逻辑保持真实。"""

    def __init__(self, artifact_root: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._artifact_root = artifact_root
        self.driver: _StubDriver | None = None

    async def open(self, url: str) -> dict[str, Any]:
        driver = _StubDriver(self._artifact_root)
        task = TaskSpec(
            "mcp-test",
            "MCP 客户端浏览器会话",
            url,
            ExecutionScope("mcp"),
            inputs=dict(self._inputs),
        )
        toolkit = BrowserToolkit(driver, task)  # type: ignore[arg-type]
        surface_id = await toolkit.open(url)
        self._toolkit = toolkit
        self.driver = driver
        return {"surface_id": surface_id, "url": url, "task_id": task.task_id}

    async def close(self) -> dict[str, Any]:
        toolkit = self._toolkit
        self._toolkit = None
        if toolkit is None:
            return {"closed": False, "reason": "当前没有浏览器会话"}
        if self.driver is not None:
            await self.driver.close()
        return {"closed": True}


def test_session_flow_from_open_to_observe_to_action(tmp_path: Path) -> None:
    session = _StubSession(tmp_path, inputs={"account": "13800138000"})
    server = McpServer(session=session, profile="core")

    opened = json.loads(
        _call(server, "open_browser", {"url": "https://shop.test/login"})["content"][0]["text"]
    )
    assert opened["surface_id"] == "surface-1"

    text = _call(server, "observe", {"as_text": True})["content"][0]["text"]
    # 候选必须逐字给出 target_id，否则客户端只能猜，猜的一定被执行层拒绝。
    assert "t-user" in text and "[textbox]" in text

    structured = json.loads(_call(server, "observe")["content"][0]["text"])
    assert structured["candidates"][0]["target_id"] == "t-user"
    assert structured["candidate_count"] == 1

    action = _call(
        server,
        "input_text",
        {"target_id": "t-user", "input_key": "account"},
    )
    assert action.get("isError") in (None, False)
    payload = json.loads(action["content"][0]["text"])
    assert payload["success"] is True
    # 敏感值只按键名引用，明文不得出现在任何返回内容里。
    assert "13800138000" not in json.dumps(action, ensure_ascii=False)
    # 动作结果自带新页面快照，客户端不必再发一次 observe 才能拿到可用的 target_id。
    assert payload["page"]["candidates"][0]["target_id"] == "t-user"
    assert payload["page"]["candidate_count"] == 1

    closed = json.loads(_call(server, "close_browser")["content"][0]["text"])
    assert closed["closed"] is True
    assert session.driver is not None and session.driver.closed is True


def test_serve_closes_the_session_when_the_client_disconnects(tmp_path: Path) -> None:
    session = _StubSession(tmp_path)
    server = McpServer(session=session, profile="core")
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "open_browser", "arguments": {"url": "https://shop.test/"}},
            }
        ),
        "",  # EOF：客户端断开
    ]
    written: list[str] = []

    async def scenario() -> None:
        queue = list(lines)

        async def read_line() -> str:
            return queue.pop(0) if queue else ""

        async def write_message(payload: str) -> None:
            written.append(payload)

        await server.serve(read_line, write_message)

    asyncio.run(scenario())

    assert len(written) == 2
    assert all(item.endswith("\n") for item in written)
    # 断开后必须把浏览器收干净，否则会留下孤儿进程。
    assert session.toolkit is None
    assert session.driver is not None and session.driver.closed is True

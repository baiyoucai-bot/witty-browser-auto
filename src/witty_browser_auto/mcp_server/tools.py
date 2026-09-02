"""对外暴露给 MCP 客户端的工具集与描述符转换。

两件事在这里定死：

- **暴露多少。** 60 个工具的 schema 会把多数 MCP 客户端的工具上下文撑爆，而一次任务
  用不到其中大半。`core` 档位是一组"一次调用干完一件事"的工具，`all` 档位给全部开放
  工具；两者都由注册表派生，不另写一份参数定义。
- **补上三个 MCP 特有的工具。** 库内的 `observe` 是门面方法而不是注册工具：Python 调用
  方拿到的是对象，而 MCP 客户端只能收文本，必须显式暴露 `observe` 才能拿到 target_id。
  会话生命周期同理——Python 调用方用 `async with`，MCP 客户端只能靠 `open_browser` /
  `close_browser` 两次调用。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from witty_browser_auto.toolkit.catalog import BROWSER_TOOLS
from witty_browser_auto.toolkit.registry import ToolDefinition

PROFILES: tuple[str, ...] = ("core", "all")

# 覆盖"看页面 / 操作页面 / 批量采集 / 逆向接口 / 排障"五类主线任务；
# 需要上传下载、会话态、环境模拟这些低频能力时用 --profile all。
CORE_TOOL_NAMES: tuple[str, ...] = (
    # 导航与页面
    "navigate",
    "navigate_history",
    "scroll",
    "wait_for_condition",
    "screenshot",
    "list_frames",
    # 元素动作
    "click",
    "click_locator",
    "input_text",
    "input_text_locator",
    "select",
    "press_key",
    "hover",
    "read_element",
    "fill_form",
    # 结构化采集
    "inspect_collection_structure",
    "run_structured_extraction",
    "replay_collection_program",
    # 网络与接口逆向
    "inspect_network_traffic",
    "search_network_traffic",
    "read_network_body",
    "analyze_api_endpoint",
    "collect_api_pages",
    "export_request_code",
    # 排障
    "inspect_page_diagnostics",
)

OPEN_BROWSER_TOOL = "open_browser"
CLOSE_BROWSER_TOOL = "close_browser"
OBSERVE_TOOL = "observe"
SESSION_TOOL_NAMES: tuple[str, ...] = (OPEN_BROWSER_TOOL, OBSERVE_TOOL, CLOSE_BROWSER_TOOL)

# MCP 客户端只能收文本，因此这三个工具的 schema 在这里手写，不走浏览器工具注册表。
SESSION_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": OPEN_BROWSER_TOOL,
        "description": (
            "启动或接管本机 Chrome 并打开入口地址，之后才能调用其它浏览器工具。"
            "同一时间只保持一个会话；重复调用会先关闭上一个会话。"
            "目标地址必须落在服务端启动时授权的 origin 内"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
                    "description": "入口页面地址，必须是 http/https 绝对地址",
                }
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": OBSERVE_TOOL,
        "description": (
            "读取当前页面并返回可操作候选清单。元素类工具的 target_id 必须逐字来自最近一次"
            "页面观察——即本工具的返回值，或任何页面动作结果里的 page 字段(两者形状相同)。"
            "页面动作成功后旧候选立即作废，但动作结果已附带新页面的 page 快照，通常不必再"
            "调本工具；只有需要更多候选、只看某几类角色或改用紧凑文本时才调。"
            "候选按置信度排序后截断，返回值会同时给出页面真实候选总数"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_candidates": {"type": "integer", "minimum": 1, "maximum": 200},
                "roles": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 40},
                    "maxItems": 20,
                    "description": '只看某几类角色，例如 ["button", "textbox"]',
                },
                "as_text": {
                    "type": "boolean",
                    "description": "为真时返回紧凑文本，否则返回结构化 JSON",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": CLOSE_BROWSER_TOOL,
        "description": "关闭当前浏览器会话并释放产物与记忆写入队列；没有会话时也安全",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
)


def profile_definitions(
    profile: str = "core",
    *,
    categories: Sequence[str] = (),
    extra_tools: Sequence[str] = (),
) -> tuple[ToolDefinition, ...]:
    """按档位与分类挑出要暴露的浏览器工具；只返回可外部调用的工具。"""

    if profile not in PROFILES:
        raise ValueError(f"未知的工具档位：{profile}，可选 {'、'.join(PROFILES)}")
    available = {item.name: item for item in BROWSER_TOOLS.externally_callable()}
    if profile == "all":
        selected = list(available.values())
    else:
        selected = [available[name] for name in CORE_TOOL_NAMES if name in available]
    for name in extra_tools:
        definition = available.get(name)
        if definition is None:
            raise ValueError(f"工具 {name} 不存在或不开放外部调用")
        if definition not in selected:
            selected.append(definition)
    if categories:
        wanted = set(categories)
        unknown = wanted - {item.category for item in available.values()}
        if unknown:
            raise ValueError(f"未知的工具分类：{'、'.join(sorted(unknown))}")
        selected = [item for item in selected if item.category in wanted]
    # 按注册表顺序输出，客户端看到的工具次序稳定。
    order = list(available)
    selected.sort(key=lambda item: order.index(item.name))
    return tuple(selected)


def mcp_descriptor(definition: ToolDefinition) -> dict[str, Any]:
    """把浏览器工具声明转成 MCP 工具描述符。

    MCP 用平铺的 `inputSchema`，OpenAI 用嵌在 `function` 里的 `parameters`；两者的参数
    体是同一份，从注册表已有的 OpenAI schema 里取即可，不必再写一遍参数定义。
    """

    schema = definition.json_schema()["function"]
    description = definition.description
    if definition.returns:
        description = f"{description}。返回：{definition.returns}"
    return {
        "name": definition.name,
        "description": description,
        "inputSchema": schema["parameters"],
    }


def tool_descriptors(definitions: Sequence[ToolDefinition]) -> tuple[dict[str, Any], ...]:
    """会话工具排在前面：客户端通常按顺序读，先看到生命周期更不容易漏掉 open_browser。"""

    return (*SESSION_TOOLS, *(mcp_descriptor(item) for item in definitions))

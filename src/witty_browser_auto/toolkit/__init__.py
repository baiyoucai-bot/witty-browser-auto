"""浏览器工具的对外契约与公共调用入口。

`BROWSER_TOOLS` 是工具名称、参数和调用限制的单一事实源；`BrowserToolkit` 让外部
智能体或脚本可以直接按工具名调用，不必了解智能体循环内部结构。

`BrowserToolkit` 采用延迟导入：它依赖执行层，而执行层的 schema 又来自本包的目录，
提前导入会形成不必要的导入环。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from witty_browser_auto.domain.errors import PolicyViolationError, RpaError
from witty_browser_auto.toolkit.catalog import BROWSER_TOOLS, CAPABILITY_AREAS
from witty_browser_auto.toolkit.registry import (
    TOOL_CATEGORIES,
    ToolArgumentError,
    ToolContractError,
    ToolDefinition,
    ToolRegistry,
)
from witty_browser_auto.toolkit.serialization import (
    observation_to_dict,
    observation_to_prompt,
    tool_result_to_dict,
)

if TYPE_CHECKING:
    from witty_browser_auto.toolkit.facade import BrowserToolkit

__all__ = [
    "BROWSER_TOOLS",
    "CAPABILITY_AREAS",
    "TOOL_CATEGORIES",
    "BrowserToolkit",
    "PolicyViolationError",
    "RpaError",
    "ToolArgumentError",
    "ToolContractError",
    "ToolDefinition",
    "ToolRegistry",
    "build_browser_toolkit",
    "describe_tools",
    "launch_browser_toolkit",
    "observation_to_dict",
    "observation_to_prompt",
    "tool_result_to_dict",
    "tool_schemas",
    "toolkit_usage_reference",
    "validate_tool_arguments",
]


def _selected_definitions(
    *,
    include_engine_only: bool,
    category: str | None,
) -> tuple[ToolDefinition, ...]:
    pool = BROWSER_TOOLS.in_category(category) if category else BROWSER_TOOLS.definitions()
    if include_engine_only:
        return tuple(pool)
    return tuple(item for item in pool if item.externally_callable)


def tool_schemas(
    *,
    include_engine_only: bool = False,
    category: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """返回 OpenAI 兼容 function schema，默认只含可外部调用的工具。

    默认排除 `finish`/`ask_user`/`block`/`wait_until`：这四个是任务终态与等待语义，
    由持有 LLM 的调用方自己决定，直接下发给模型只会换来一次被拒绝的无效回合。
    生成文档或做契约兼容性校验时传 `include_engine_only=True` 取全量。
    """

    return tuple(
        definition.json_schema()
        for definition in _selected_definitions(
            include_engine_only=include_engine_only,
            category=category,
        )
    )


def describe_tools(
    *,
    include_engine_only: bool = False,
    category: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """返回可读工具契约，默认只含可外部调用的工具，供生成调用代码或文档。"""

    return tuple(
        definition.describe()
        for definition in _selected_definitions(
            include_engine_only=include_engine_only,
            category=category,
        )
    )


def validate_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """在真正执行前校验工具参数，把可预见的错误留在调用方本地。"""

    return BROWSER_TOOLS.validate_arguments(name, arguments)


def __getattr__(name: str) -> Any:
    if name == "BrowserToolkit":
        from witty_browser_auto.toolkit.facade import BrowserToolkit as _BrowserToolkit

        return _BrowserToolkit
    if name in {"build_browser_toolkit", "launch_browser_toolkit", "toolkit_usage_reference"}:
        # 装配入口依赖浏览器与网络层，按需导入避免把重依赖带进纯契约查询场景。
        from witty_browser_auto.toolkit import bootstrap

        return getattr(bootstrap, name)
    raise AttributeError(f"witty_browser_auto.toolkit 没有属性 {name}")

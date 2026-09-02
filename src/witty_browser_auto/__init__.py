"""Witty 浏览器工具库。

面向自带大模型的外部智能体框架：`launch_browser_toolkit` 打开会话，`tool_schemas`
给出可直接下发的工具定义，`observation_to_*`/`tool_result_to_dict` 负责把观察与
工具结果转成可进模型上下文的结构。全部导出按需加载，纯契约查询不会拉起浏览器层。
"""

from typing import Any

__all__ = [
    "BROWSER_TOOLS",
    "BrowserToolkit",
    "PolicyViolationError",
    "RpaError",
    "ToolArgumentError",
    "build_browser_toolkit",
    "describe_tools",
    "launch_browser_toolkit",
    "observation_to_dict",
    "observation_to_prompt",
    "tool_result_to_dict",
    "tool_schemas",
    "validate_tool_arguments",
]
__version__ = "0.1.0"


def __getattr__(name: str) -> Any:
    if name in __all__:
        from witty_browser_auto import toolkit

        return getattr(toolkit, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

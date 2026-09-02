"""动作脚本导出的执行层。

脚本正文只回给调用方进程，模型侧只拿到步骤清单与统计，与其余"完整数据/有界视图"
双路工具保持同一条界线。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from witty_browser_auto.domain.models import TaskSpec
from witty_browser_auto.toolkit.script_export import (
    SCRIPT_TARGETS,
    ActionScriptLog,
    build_action_script,
)

SCRIPT_TOOL_NAMES = frozenset({"export_action_script"})


@dataclass(frozen=True, slots=True)
class ScriptToolOutcome:
    message: str
    data: dict[str, Any]
    model_data: dict[str, Any]


def execute_script_tool(
    arguments: Mapping[str, Any],
    *,
    log: ActionScriptLog,
    task: TaskSpec,
) -> ScriptToolOutcome:
    unknown = set(arguments) - {"target"}
    if unknown:
        raise ValueError(f"export_action_script 包含未知参数：{', '.join(sorted(unknown))}")
    target = arguments.get("target", "python_toolkit")
    if not isinstance(target, str) or target not in SCRIPT_TARGETS:
        raise ValueError(f"不支持的脚本目标：{target}")

    steps = tuple(log.steps)
    input_keys = log.referenced_input_keys()
    code = build_action_script(steps, task=task, input_keys=input_keys, target=target)
    unresolved = [step.public_dict() for step in steps if step.notes]
    data = {
        "target": target,
        "code": code,
        "step_count": len(steps),
        "steps": [step.public_dict() for step in steps],
        "input_keys": list(input_keys),
        "needs_manual_review": unresolved,
    }
    model_data = {
        "target": target,
        "step_count": len(steps),
        "tools": [step.tool for step in steps],
        "input_keys": list(input_keys),
        "needs_manual_review_count": len(unresolved),
        "code_chars": len(code),
        "note": "脚本正文只返回给调用方进程，模型侧只提供步骤清单与统计",
    }
    message = f"已导出 {len(steps)} 步动作脚本，共 {len(code)} 字符"
    if unresolved:
        message += f"；其中 {len(unresolved)} 步需要人工复核定位器"
    return ScriptToolOutcome(message=message, data=data, model_data=model_data)

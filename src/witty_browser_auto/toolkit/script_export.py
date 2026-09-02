"""把已成功执行的浏览器动作导出成可独立重跑的脚本。

和"录制页面事件再猜选择器"不同，这里的每一步都来自真实执行并通过了业务后置校验，
定位器则由当轮观察中命中的那个候选反推。代价是观察候选的 `target_id` 带会话版本号，
重跑必然失效，因此录制时就要把它换成 test-id、id、role+name 这类跨会话稳定的定位器。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from witty_browser_auto.domain.models import CandidateTarget, Observation, TaskSpec

SCRIPT_TARGETS: tuple[str, ...] = ("python_toolkit",)

_MAX_STEPS = 500
_CSS_IDENTIFIER = re.compile(r"^[A-Za-z_][\w-]*$")
_MAX_TEXT_LOCATOR = 80

# 这些参数是执行期状态，不能照抄进脚本：指纹由门面在重跑时重新绑定。
_EPHEMERAL_ARGUMENTS = frozenset({"observation_fingerprint"})


@dataclass(frozen=True, slots=True)
class ActionStep:
    """一步已验证动作及其可重跑形态。"""

    index: int
    tool: str
    arguments: dict[str, Any]
    description: str
    notes: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "description": self.description,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class ActionScriptLog:
    """按执行顺序记录已成功且改变了页面状态的动作。"""

    steps: list[ActionStep] = field(default_factory=list)

    def record(
        self,
        *,
        tool: str,
        arguments: Mapping[str, Any],
        observation: Observation | None,
        success: bool,
        counts_as_action: bool,
    ) -> None:
        # 只留改变了页面状态的成功动作：读取与诊断不影响重跑结果，记进来只会是噪声。
        if not success or not counts_as_action:
            return
        if len(self.steps) >= _MAX_STEPS:
            self.steps.pop(0)
        replayable, description, notes = _to_replayable(tool, arguments, observation)
        self.steps.append(
            ActionStep(
                index=len(self.steps) + 1,
                tool=replayable[0],
                arguments=replayable[1],
                description=description,
                notes=notes,
            )
        )

    def clear(self) -> None:
        self.steps.clear()

    def referenced_input_keys(self) -> tuple[str, ...]:
        keys: list[str] = []
        for step in self.steps:
            for name, value in step.arguments.items():
                if not name.endswith(("input_key", "input_keys")):
                    continue
                if isinstance(value, Mapping):
                    # Header 类参数是「Header 名 → 输入键」的映射，取值才是输入键。
                    referenced: list[Any] = list(value.values())
                elif isinstance(value, list):
                    referenced = list(value)
                else:
                    referenced = [value]
                for key in referenced:
                    if isinstance(key, str) and key not in keys:
                        keys.append(key)
        return tuple(keys)


# ----------------------------------------------------------------------
# 定位器反推
# ----------------------------------------------------------------------


def derive_locator(candidate: CandidateTarget) -> tuple[dict[str, Any] | None, str]:
    """从命中的观察候选反推一个跨会话稳定的显式定位器。"""

    descriptor = _decode(candidate.recipe.value)
    attrs = descriptor.get("attrs") if isinstance(descriptor.get("attrs"), Mapping) else {}
    tag = str(descriptor.get("tag", ""))

    test_id = attrs.get("data-testid")
    if isinstance(test_id, str) and test_id.strip():
        return {"strategy": "test_id", "value": test_id.strip()}, ""

    element_id = attrs.get("id")
    if isinstance(element_id, str) and _CSS_IDENTIFIER.match(element_id.strip()):
        return {"strategy": "css", "value": f"#{element_id.strip()}"}, ""

    selector = descriptor.get("selector")
    if isinstance(selector, str) and selector.strip():
        return {"strategy": "css", "value": selector.strip()}, ""

    if candidate.role and candidate.name:
        return {
            "strategy": "role",
            "value": candidate.role,
            "name": candidate.name,
        }, ""

    field_name = attrs.get("name")
    if isinstance(field_name, str) and field_name.strip() and tag:
        escaped = json.dumps(field_name.strip())
        return {"strategy": "css", "value": f"{tag}[name={escaped}]"}, ""

    text = str(descriptor.get("text", "")).strip()
    if text:
        return {"strategy": "text", "value": text[:_MAX_TEXT_LOCATOR]}, ""

    return None, "该候选没有 test-id、id、role+name 或文本可用，需要手工补定位器"


def _decode(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


# ----------------------------------------------------------------------
# 单步改写
# ----------------------------------------------------------------------

# 只给"必须换成定位器"的工具做改名；其余工具原样输出即可重跑。
_LOCATOR_EQUIVALENT = {
    "click": "click_locator",
    "input_text": "input_text_locator",
    "select": "select_locator",
}


def _to_replayable(
    tool: str,
    arguments: Mapping[str, Any],
    observation: Observation | None,
) -> tuple[tuple[str, dict[str, Any]], str, tuple[str, ...]]:
    payload = {key: value for key, value in arguments.items() if key not in _EPHEMERAL_ARGUMENTS}
    notes: list[str] = []
    description = tool

    target_id = payload.pop("target_id", None)
    if isinstance(target_id, str):
        candidate = _find_candidate(observation, target_id)
        if candidate is None:
            payload["target_id"] = target_id
            notes.append("未能在当轮观察中找到该候选，target_id 跨会话失效，需要手工补定位器")
        else:
            locator, reason = derive_locator(candidate)
            label = candidate.name or candidate.text
            description = f"{tool} → {candidate.role or '元素'}「{label}」"
            if locator is None:
                payload["target_id"] = target_id
                notes.append(reason)
            else:
                payload["locator"] = locator
                tool = _LOCATOR_EQUIVALENT.get(tool, tool)

    if tool == "fill_form":
        _rewrite_form_field_locators(payload, observation, notes)
    elif tool == "drag_to_element":
        _rewrite_drag_endpoint_locators(payload, observation, notes)

    expect_kind = payload.get("expect_kind")
    if expect_kind == "fingerprint_changed":
        # 指纹是执行期值，门面在重跑时会重新绑定当前观察。
        payload.pop("expect_value", None)
    elif expect_kind == "target_exists":
        expect_value = payload.get("expect_value")
        candidate = _find_candidate(observation, expect_value)
        replacement = None
        if candidate is not None:
            replacement, _ = derive_locator(candidate)
        if replacement is None:
            payload["expect_kind"] = "fingerprint_changed"
            payload.pop("expect_value", None)
            notes.append(
                "原后置条件是 target_exists，其取值是会话内 target_id，已降级为页面变化校验"
            )

    return (tool, payload), description, tuple(notes)


def _rewrite_form_field_locators(
    payload: dict[str, Any],
    observation: Observation | None,
    notes: list[str],
) -> None:
    fields = payload.get("fields")
    if not isinstance(fields, list):
        return
    rewritten_fields: list[Any] = []
    for index, raw_field in enumerate(fields):
        if not isinstance(raw_field, Mapping):
            rewritten_fields.append(raw_field)
            continue
        field = dict(raw_field)
        target_id = field.pop("target_id", None)
        if not isinstance(target_id, str):
            rewritten_fields.append(field)
            continue
        candidate = _find_candidate(observation, target_id)
        locator, reason = derive_locator(candidate) if candidate is not None else (None, "")
        if locator is None:
            field["target_id"] = target_id
            notes.append(
                f"表单第 {index + 1} 个字段无法导出稳定定位器：{reason or '未找到当轮观察候选'}"
            )
        else:
            field["locator"] = locator
        rewritten_fields.append(field)
    payload["fields"] = rewritten_fields


def _rewrite_drag_endpoint_locators(
    payload: dict[str, Any],
    observation: Observation | None,
    notes: list[str],
) -> None:
    for side in ("source", "target"):
        target_key = f"{side}_target_id"
        locator_key = f"{side}_locator"
        target_id = payload.pop(target_key, None)
        if not isinstance(target_id, str):
            continue
        candidate = _find_candidate(observation, target_id)
        locator, reason = derive_locator(candidate) if candidate is not None else (None, "")
        if locator is None:
            payload[target_key] = target_id
            notes.append(f"拖放{side}端无法导出稳定定位器：{reason or '未找到当轮观察候选'}")
        else:
            payload[locator_key] = locator


def _find_candidate(observation: Observation | None, target_id: Any) -> CandidateTarget | None:
    if observation is None or not isinstance(target_id, str):
        return None
    for candidate in observation.candidates:
        if candidate.target_id == target_id:
            return candidate
    return None


# ----------------------------------------------------------------------
# 脚本渲染
# ----------------------------------------------------------------------


def build_action_script(
    steps: Sequence[ActionStep],
    *,
    task: TaskSpec,
    input_keys: Sequence[str] = (),
    target: str = "python_toolkit",
) -> str:
    if target not in SCRIPT_TARGETS:
        raise ValueError(f"不支持的脚本目标：{target}")
    if not steps:
        raise ValueError("本次会话还没有可导出的已验证动作")

    origins = list(task.scope.allowed_origins) or [_origin_of(task.start_url)]
    lines = [
        '"""由Witty 浏览器工具从已执行动作导出的可重跑脚本。',
        "",
        f"来源任务：{task.goal}",
        f"步骤数：{len(steps)}",
        "",
        "每一步都来自真实执行并通过了业务后置校验；元素定位器由当时命中的候选反推，",
        "因此不含跨会话失效的 target_id。重跑前请把 INPUTS 里的占位值换成真实值。",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import asyncio",
        "",
        "from witty_browser_auto.toolkit import launch_browser_toolkit",
        "",
        f"START_URL = {json.dumps(task.start_url)}",
        f"ALLOWED_ORIGINS = {json.dumps(origins, ensure_ascii=False)}",
    ]

    if input_keys:
        lines.append("INPUTS = {")
        for key in input_keys:
            lines.append(f'    {json.dumps(key, ensure_ascii=False)}: "",  # 请填入真实值')
        lines.append("}")
    else:
        lines.append("INPUTS: dict[str, str] = {}")

    lines += [
        "",
        "",
        "async def main() -> None:",
        "    async with launch_browser_toolkit(",
        "        START_URL,",
        "        allowed_origins=ALLOWED_ORIGINS,",
        "        inputs=INPUTS,",
        "    ) as toolkit:",
    ]

    for position, step in enumerate(steps):
        if position:
            lines.append("")
        lines.append(f"        # 第 {step.index} 步：{step.description}")
        for note in step.notes:
            lines.append(f"        # 注意：{note}")
        lines.append("        result = await toolkit.call(")
        lines.append(f"            {json.dumps(step.tool)},")
        for name, value in step.arguments.items():
            rendered = _render(value, indent=12)
            lines.append(f"            {name}={rendered},")
        lines.append("        )")
        lines.append("        assert result.success, result.message")

    lines += [
        "",
        "",
        'if __name__ == "__main__":',
        "    asyncio.run(main())",
        "",
    ]
    return "\n".join(lines)


def _render(value: Any, *, indent: int) -> str:
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    rendered = json.dumps(value, ensure_ascii=False, indent=4)
    lines = rendered.splitlines()
    if len(lines) == 1:
        return rendered
    # json 自带的 4 空格是相对缩进，这里再补上宿主实参所在列，闭合括号才会对齐。
    pad = " " * indent
    return "\n".join([lines[0], *(f"{pad}{line}" for line in lines[1:])])


def _origin_of(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else url

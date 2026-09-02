"""表单批量填写、独立等待与会话态存取的执行层。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from witty_browser_auto.agent.locator_tools import locator_recipe
from witty_browser_auto.browser.form_fill import MAX_FIELDS, MAX_TEXT_LENGTH, FormField
from witty_browser_auto.browser.storage_state import (
    read_state_file,
    summarize,
    write_state_file,
)
from witty_browser_auto.domain.models import ExpectedCondition, TaskSpec
from witty_browser_auto.domain.protocols import AutomationDriver

FORM_TOOL_NAMES = frozenset({"fill_form", "wait_for_condition", "manage_storage_state"})

_WAIT_KINDS: tuple[str, ...] = (
    "url_contains",
    "title_contains",
    "text_contains",
    "fingerprint_changed",
)
_STATE_OPERATIONS: tuple[str, ...] = ("export", "import")

_FIELD_KEYS = frozenset({"target_id", "locator", "input_key", "text", "select_value", "checked"})


@dataclass(frozen=True, slots=True)
class FormToolOutcome:
    success: bool
    message: str
    data: dict[str, Any]
    model_data: dict[str, Any] | None = None


def forms_available(driver: AutomationDriver) -> bool:
    capabilities = getattr(driver, "capabilities", None)
    return bool(getattr(capabilities, "forms", False)) and hasattr(driver, "fill_fields")


def storage_state_available(driver: AutomationDriver) -> bool:
    capabilities = getattr(driver, "capabilities", None)
    return bool(getattr(capabilities, "storage_state", False)) and hasattr(
        driver, "export_storage_state"
    )


# ----------------------------------------------------------------------
# fill_form
# ----------------------------------------------------------------------


async def execute_fill_form(
    arguments: Mapping[str, Any],
    *,
    driver: AutomationDriver,
    task: TaskSpec,
) -> FormToolOutcome:
    if not forms_available(driver):
        raise ValueError("当前驱动不支持表单批量填写")
    unknown = set(arguments) - {"fields"}
    if unknown:
        raise ValueError(f"fill_form 包含未知参数：{', '.join(sorted(unknown))}")
    raw_fields = arguments.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError("fields 必须是非空数组")
    if len(raw_fields) > MAX_FIELDS:
        raise ValueError(f"一次最多填写 {MAX_FIELDS} 个字段")

    fields = [_build_field(index, item, task) for index, item in enumerate(raw_fields)]
    results = await driver.fill_fields(fields)
    filled = [item for item in results if item.get("filled")]
    failed = [item for item in results if not item.get("filled")]
    data = {
        "fields": results,
        "filled_count": len(filled),
        "failed_count": len(failed),
    }
    if failed:
        reasons = "；".join(
            f"第 {item.get('index')} 个字段：{item.get('reason', '未知原因')}"
            for item in failed[:5]
        )
        message = f"{len(filled)} 个字段写入成功，{len(failed)} 个失败。{reasons}"
    else:
        message = f"{len(filled)} 个字段全部写入并回读校验通过"
    return FormToolOutcome(success=not failed, message=message, data=data)


def _build_field(index: int, raw: Any, task: TaskSpec) -> FormField:
    if not isinstance(raw, Mapping):
        raise ValueError(f"第 {index} 个字段必须是对象")
    unknown = set(raw) - _FIELD_KEYS
    if unknown:
        raise ValueError(f"第 {index} 个字段包含未知参数：{', '.join(sorted(unknown))}")

    has_target = bool(isinstance(raw.get("target_id"), str) and raw["target_id"].strip())
    has_locator = raw.get("locator") is not None
    if has_target == has_locator:
        raise ValueError(f"第 {index} 个字段必须且只能给出 target_id 或 locator 之一")
    locator = locator_recipe(raw) if has_locator else None
    target_id = str(raw["target_id"]).strip() if has_target else None

    provided = [key for key in ("input_key", "text", "select_value", "checked") if key in raw]
    if len(provided) != 1:
        raise ValueError(
            f"第 {index} 个字段必须且只能给出 input_key、text、select_value、checked 之一"
        )
    source = provided[0]
    if source == "input_key":
        key = raw["input_key"]
        if not isinstance(key, str) or key not in task.inputs:
            raise ValueError(f"第 {index} 个字段引用的任务输入键不存在：{key}")
        value = task.inputs[key]
        if not isinstance(value, str):
            raise ValueError(f"第 {index} 个字段的任务输入不是字符串：{key}")
        return FormField(
            index, "text", value=value, target_id=target_id, locator=locator, sensitive=True
        )
    if source == "text":
        value = raw["text"]
        if not isinstance(value, str):
            raise ValueError(f"第 {index} 个字段的 text 必须是字符串")
        if len(value) > MAX_TEXT_LENGTH:
            raise ValueError(f"第 {index} 个字段的 text 超过 {MAX_TEXT_LENGTH} 字符上限")
        return FormField(index, "text", value=value, target_id=target_id, locator=locator)
    if source == "select_value":
        value = raw["select_value"]
        if not isinstance(value, str) or not value:
            raise ValueError(f"第 {index} 个字段的 select_value 必须是非空字符串")
        return FormField(index, "select", value=value, target_id=target_id, locator=locator)
    checked = raw["checked"]
    if not isinstance(checked, bool):
        raise ValueError(f"第 {index} 个字段的 checked 必须是布尔值")
    return FormField(index, "checkbox", checked=checked, target_id=target_id, locator=locator)


# ----------------------------------------------------------------------
# wait_for_condition
# ----------------------------------------------------------------------


async def execute_wait_for_condition(
    arguments: Mapping[str, Any],
    *,
    driver: AutomationDriver,
) -> FormToolOutcome:
    unknown = set(arguments) - {"expect_kind", "expect_value", "timeout_seconds"}
    if unknown:
        raise ValueError(f"wait_for_condition 包含未知参数：{', '.join(sorted(unknown))}")
    kind = arguments.get("expect_kind")
    if kind not in _WAIT_KINDS:
        raise ValueError(f"expect_kind 只能是 {'、'.join(_WAIT_KINDS)}")
    value = arguments.get("expect_value")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expect_value 必须是非空字符串")
    timeout = arguments.get("timeout_seconds", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise ValueError("timeout_seconds 必须是数字")
    if not 0.1 <= float(timeout) <= 300:
        raise ValueError("timeout_seconds 必须在 0.1 到 300 秒之间")

    condition = ExpectedCondition(kind, value.strip(), timeout_seconds=float(timeout))
    outcome = await driver.wait_for(condition)
    satisfied = bool(outcome.get("satisfied"))
    waited = outcome.get("waited_seconds")
    message = (
        f"等待条件已满足，用时 {waited} 秒"
        if satisfied
        else f"等待 {waited} 秒后条件仍未满足：{outcome.get('message', '')}"
    )
    return FormToolOutcome(success=satisfied, message=message, data=dict(outcome))


# ----------------------------------------------------------------------
# manage_storage_state
# ----------------------------------------------------------------------


async def execute_storage_state(
    arguments: Mapping[str, Any],
    *,
    driver: AutomationDriver,
    task: TaskSpec,
    artifact_root: Path,
) -> FormToolOutcome:
    if not storage_state_available(driver):
        raise ValueError("当前驱动不支持会话态整体存取")
    unknown = set(arguments) - {"operation", "file_path", "state", "clear_existing"}
    if unknown:
        raise ValueError(f"manage_storage_state 包含未知参数：{', '.join(sorted(unknown))}")
    operation = arguments.get("operation")
    if operation not in _STATE_OPERATIONS:
        raise ValueError(f"operation 只能是 {'、'.join(_STATE_OPERATIONS)}")

    allowed = {origin.rstrip("/") for origin in task.scope.allowed_origins}
    if operation == "export":
        if "state" in arguments or "clear_existing" in arguments:
            raise ValueError("导出会话态不接受 state 或 clear_existing")
        urls = sorted(allowed) or [task.start_url]
        state = await driver.export_storage_state(urls=urls)
        path = write_state_file(state, artifact_root / "storage-state")
        summary = summarize(state)
        return FormToolOutcome(
            success=True,
            message=(
                f"已导出 {summary['cookie_count']} 个 Cookie 与 "
                f"{len(summary['origins'])} 个 origin 的 Web Storage 到 {path}"
            ),
            # 快照里通常含会话凭据，只有外部调用方拿得到正文。
            data={"state": state, "file_path": str(path), "summary": summary},
            model_data={"file_path": str(path), "summary": summary},
        )

    state = _resolve_import_state(arguments)
    clear_existing = arguments.get("clear_existing", False)
    if not isinstance(clear_existing, bool):
        raise ValueError("clear_existing 必须是布尔值")
    outcome = await driver.import_storage_state(
        state, allowed_origins=allowed, clear_existing=clear_existing
    )
    skipped = outcome.get("cookies_skipped") or []
    message = (
        f"已导入 {outcome.get('cookies_applied', 0)} 个 Cookie、"
        f"localStorage {outcome['storage_written']['localStorage']} 项、"
        f"sessionStorage {outcome['storage_written']['sessionStorage']} 项"
    )
    if skipped:
        message += f"；{len(skipped)} 个 Cookie 因超出任务授权范围被跳过"
    if outcome.get("origins_skipped"):
        message += (
            f"；{len(outcome['origins_skipped'])} 个 origin 的 Web Storage 需切到该页面后再导入"
        )
    return FormToolOutcome(success=True, message=message, data=dict(outcome))


def _resolve_import_state(arguments: Mapping[str, Any]) -> dict[str, Any]:
    inline = arguments.get("state")
    file_path = arguments.get("file_path")
    if (inline is None) == (file_path is None):
        raise ValueError("导入会话态必须且只能给出 state 或 file_path 之一")
    if file_path is not None:
        if not isinstance(file_path, str) or not file_path.strip():
            raise ValueError("file_path 必须是非空字符串")
        return read_state_file(Path(file_path).expanduser())
    if not isinstance(inline, Mapping):
        raise ValueError("state 必须是对象")
    return dict(inline)

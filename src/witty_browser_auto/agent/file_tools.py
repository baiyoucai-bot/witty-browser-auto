"""文件上传与下载工具的执行层。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from witty_browser_auto.agent.locator_tools import locator_recipe
from witty_browser_auto.browser.files import resolve_upload_paths
from witty_browser_auto.domain.errors import PolicyViolationError
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionKind,
    ExpectedCondition,
    LocatorRecipe,
    ModelToolCall,
    TaskSpec,
)
from witty_browser_auto.domain.protocols import AutomationDriver, DownloadInspectionProvider
from witty_browser_auto.security.redaction import redact_task_inputs
from witty_browser_auto.toolkit.catalog import FILE_TOOLS, names_of, schemas_of

FILE_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = schemas_of(FILE_TOOLS)
FILE_TOOL_NAMES = names_of(FILE_TOOLS)
UPLOAD_TOOL_NAMES = frozenset({"upload_files"})
DOWNLOAD_TOOL_NAMES = frozenset({"list_downloads", "wait_for_download"})


@dataclass(frozen=True, slots=True)
class FileToolOutcome:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


def downloads_available(driver: AutomationDriver) -> bool:
    return isinstance(driver, DownloadInspectionProvider)


def resolve_upload_path_arguments(
    arguments: Mapping[str, Any],
    task_inputs: Mapping[str, Any],
) -> list[str]:
    """合并 paths 与 path_input_keys，并做存在性与类型校验。"""

    raw_paths = arguments.get("paths")
    raw_keys = arguments.get("path_input_keys")
    paths: list[str] = []
    if raw_paths is not None:
        if not isinstance(raw_paths, list) or not all(isinstance(item, str) for item in raw_paths):
            raise ValueError("paths 必须是字符串数组")
        paths.extend(raw_paths)
    if raw_keys is not None:
        if not isinstance(raw_keys, list) or not all(isinstance(item, str) for item in raw_keys):
            raise ValueError("path_input_keys 必须是字符串数组")
        for key in raw_keys:
            if key not in task_inputs:
                raise PolicyViolationError(f"任务输入键不存在：{key}")
            value = task_inputs[key]
            if not isinstance(value, str) or not value.strip():
                raise PolicyViolationError(f"任务输入键 {key} 的值必须是文件路径字符串")
            paths.append(value)
    if not paths:
        raise ValueError("必须提供 paths 或 path_input_keys 之一")
    return [str(path) for path in resolve_upload_paths(paths)]


def build_upload_command(
    call: ModelToolCall,
    *,
    task: TaskSpec,
    action_id: str,
    observation_fingerprint: str | None = None,
) -> tuple[ActionCommand, tuple[str, ...]]:
    arguments = call.arguments
    target_id = arguments.get("target_id")
    locator_raw = arguments.get("locator")
    if target_id is not None and not isinstance(target_id, str):
        raise ValueError("target_id 必须是字符串")
    locator: LocatorRecipe | None = None
    if locator_raw is not None:
        if not isinstance(locator_raw, Mapping):
            raise ValueError("locator 必须是对象")
        locator = locator_recipe({"locator": dict(locator_raw)})
    if bool(target_id) == bool(locator):
        raise ValueError("upload_files 必须且只能提供 target_id 或 locator 之一")

    paths = resolve_upload_path_arguments(arguments, task.inputs)
    expected = _optional_expected(arguments, observation_fingerprint)
    command = ActionCommand(
        action_id,
        ActionKind.UPLOAD_FILES,
        target_id=target_id if isinstance(target_id, str) else None,
        locator=locator,
        file_paths=tuple(paths),
        expected=expected,
        idempotent=False,
    )
    return command, tuple(paths)


async def execute_download_tool(
    call: ModelToolCall,
    driver: AutomationDriver,
    *,
    task_inputs: Mapping[str, Any],
) -> FileToolOutcome:
    if call.name not in DOWNLOAD_TOOL_NAMES:
        raise ValueError(f"未知下载工具：{call.name}")
    if not isinstance(driver, DownloadInspectionProvider):
        return FileToolOutcome(False, "当前浏览器表面没有下载跟踪能力")
    if call.name == "list_downloads":
        limit = call.arguments.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit 必须是整数")
        records = await driver.list_downloads(limit=limit)
        safe = [_safe_download(record, task_inputs) for record in records]
        return FileToolOutcome(True, f"已列出 {len(safe)} 条下载", {"downloads": safe})

    suggested = call.arguments.get("suggested_filename")
    url_contains = call.arguments.get("url_contains")
    timeout = call.arguments.get("timeout_seconds", 30)
    if suggested is not None and not isinstance(suggested, str):
        raise ValueError("suggested_filename 必须是字符串")
    if url_contains is not None and not isinstance(url_contains, str):
        raise ValueError("url_contains 必须是字符串")
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise ValueError("timeout_seconds 必须是数字")
    try:
        record = await driver.wait_for_download(
            suggested_filename=suggested,
            url_contains=url_contains,
            timeout_seconds=float(timeout),
        )
    except TimeoutError:
        return FileToolOutcome(False, "等待下载超时")
    except ValueError as exc:
        return FileToolOutcome(False, str(exc))
    return FileToolOutcome(True, "下载已完成", _safe_download(record, task_inputs))


def _optional_expected(
    arguments: Mapping[str, Any],
    observation_fingerprint: str | None,
) -> ExpectedCondition | None:
    kind = arguments.get("expect_kind")
    value = arguments.get("expect_value")
    if kind is None and value is None:
        return None
    if not isinstance(kind, str) or not isinstance(value, str):
        raise ValueError("expect_kind 与 expect_value 必须同时为字符串")
    if kind == "fingerprint_changed" and not value and observation_fingerprint:
        value = observation_fingerprint
    return ExpectedCondition(kind=kind, value=value)


def _safe_download(
    record: Mapping[str, Any],
    task_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    redacted = redact_task_inputs(dict(record), dict(task_inputs))
    return redacted if isinstance(redacted, dict) else {}

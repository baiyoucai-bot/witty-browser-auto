"""Cookie 与 Web Storage 工具的执行层。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from witty_browser_auto.domain.errors import PolicyViolationError
from witty_browser_auto.domain.models import ModelToolCall, TaskSpec
from witty_browser_auto.domain.protocols import AutomationDriver, StorageInspectionProvider
from witty_browser_auto.memory.url import normalize_url
from witty_browser_auto.security.redaction import REDACTED, redact_task_inputs
from witty_browser_auto.toolkit.catalog import STORAGE_TOOLS, names_of, schemas_of

STORAGE_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = schemas_of(STORAGE_TOOLS)
STORAGE_TOOL_NAMES = names_of(STORAGE_TOOLS)
READ_STORAGE_TOOL_NAMES = frozenset({"read_cookies", "read_web_storage"})
WRITE_STORAGE_TOOL_NAMES = frozenset({"set_cookie", "write_web_storage"})


@dataclass(frozen=True, slots=True)
class StorageToolOutcome:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    model_data: dict[str, Any] = field(default_factory=dict)
    idempotent: bool = True
    counts_as_action: bool = False


def storage_available(driver: AutomationDriver) -> bool:
    return isinstance(driver, StorageInspectionProvider)


def effective_allowed_origins(task: TaskSpec) -> tuple[str, ...]:
    if task.scope.allowed_origins:
        return task.scope.allowed_origins
    return (normalize_url(task.start_url).origin,)


async def execute_storage_tool(
    call: ModelToolCall,
    driver: AutomationDriver,
    *,
    task: TaskSpec,
    task_inputs: Mapping[str, Any],
) -> StorageToolOutcome:
    if call.name not in STORAGE_TOOL_NAMES:
        raise ValueError(f"未知存储工具：{call.name}")
    if not isinstance(driver, StorageInspectionProvider):
        return StorageToolOutcome(False, "当前浏览器表面没有存储读写能力")

    origins = effective_allowed_origins(task)
    if call.name == "read_cookies":
        url = await _resolve_url(call.arguments, driver, task, origins)
        names = _optional_name_filter(call.arguments.get("names"))
        cookies = await driver.read_cookies(url, names=names)
        full = {"url": url, "cookies": cookies, "count": len(cookies)}
        return StorageToolOutcome(
            success=True,
            message=f"已读取 {len(cookies)} 条 Cookie",
            data=full,
            model_data=_safe_cookies_for_model(full, task_inputs),
            idempotent=True,
            counts_as_action=False,
        )

    if call.name == "set_cookie":
        url = await _resolve_url(call.arguments, driver, task, origins)
        name = _required_text(call.arguments, "name", 256)
        value = _resolve_secret_value(call.arguments, task_inputs)
        path = _optional_text(call.arguments, "path", 1024) or "/"
        domain = _optional_text(call.arguments, "domain", 253)
        http_only = bool(call.arguments.get("http_only", False))
        secure = bool(call.arguments.get("secure", False))
        expires = call.arguments.get("expires")
        if expires is not None and (
            isinstance(expires, bool) or not isinstance(expires, int | float)
        ):
            raise ValueError("expires 必须是数字")
        result = await driver.set_cookie(
            name=name,
            value=value,
            url=url,
            path=path,
            domain=domain,
            http_only=http_only,
            secure=secure,
            expires=float(expires) if expires is not None else None,
        )
        safe = redact_task_inputs(result, task_inputs)
        return StorageToolOutcome(
            success=True,
            message=f"已写入 Cookie：{name}",
            data=safe if isinstance(safe, dict) else {},
            model_data=safe if isinstance(safe, dict) else {},
            idempotent=False,
            counts_as_action=False,
        )

    frame_id = _optional_text(call.arguments, "frame_id", 100)
    storage_kind = _required_storage_kind(call.arguments)

    if call.name == "read_web_storage":
        key = _optional_text(call.arguments, "key", 256)
        result = await driver.read_web_storage(
            storage_kind=storage_kind,
            key=key,
            frame_id=frame_id,
        )
        return StorageToolOutcome(
            success=True,
            message=(
                f"已列出 {len(result.get('keys', []))} 个 {storage_kind}Storage 键"
                if result.get("mode") == "keys"
                else f"已读取 {storage_kind}Storage 键 {key}"
            ),
            data=result,
            model_data=_safe_storage_for_model(result, task_inputs),
            idempotent=True,
            counts_as_action=False,
        )

    key = _required_text(call.arguments, "key", 256)
    remove = bool(call.arguments.get("remove", False))
    value = None if remove else _resolve_secret_value(call.arguments, task_inputs)
    result = await driver.write_web_storage(
        storage_kind=storage_kind,
        key=key,
        value=value,
        frame_id=frame_id,
        remove=remove,
    )
    safe = redact_task_inputs(result, task_inputs)
    return StorageToolOutcome(
        success=True,
        message=f"已{'删除' if remove else '写入'} {storage_kind}Storage 键 {key}",
        data=safe if isinstance(safe, dict) else {},
        model_data=safe if isinstance(safe, dict) else {},
        idempotent=False,
        counts_as_action=False,
    )


async def _resolve_url(
    arguments: Mapping[str, Any],
    driver: StorageInspectionProvider,
    task: TaskSpec,
    origins: tuple[str, ...],
) -> str:
    raw = arguments.get("url")
    if raw is None:
        url = await driver.current_page_url()
        if not url:
            url = task.start_url
    else:
        url = _required_text(arguments, "url", 4096)
    from witty_browser_auto.browser.storage import allowed_origins_for_url

    allowed_origins_for_url(url, origins)
    return url


def _resolve_secret_value(arguments: Mapping[str, Any], task_inputs: Mapping[str, Any]) -> str:
    inline = arguments.get("value")
    key = arguments.get("value_input_key")
    if inline is not None and key is not None:
        raise ValueError("value 与 value_input_key 只能二选一")
    if key is not None:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("value_input_key 必须是非空字符串")
        if key not in task_inputs:
            raise PolicyViolationError(f"任务输入键不存在：{key}")
        resolved = task_inputs[key]
        if not isinstance(resolved, str):
            raise PolicyViolationError(f"任务输入键 {key} 的值必须是字符串")
        return resolved
    if inline is not None:
        if not isinstance(inline, str):
            raise ValueError("value 必须是字符串")
        return inline
    raise ValueError("必须提供 value 或 value_input_key 之一")


def _optional_name_filter(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError("names 必须是字符串数组")
    if len(raw) > 50:
        raise ValueError("names 最多 50 项")
    return list(raw)


def _required_storage_kind(arguments: Mapping[str, Any]) -> str:
    kind = arguments.get("storage_kind")
    if kind not in {"local", "session"}:
        raise ValueError("storage_kind 必须是 local 或 session")
    return kind


def _required_text(arguments: Mapping[str, Any], key: str, limit: int) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    if len(value) > limit:
        raise ValueError(f"{key} 超过 {limit} 字符上限")
    return value


def _optional_text(arguments: Mapping[str, Any], key: str, limit: int) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")
    if len(value) > limit:
        raise ValueError(f"{key} 超过 {limit} 字符上限")
    return value


def _safe_cookies_for_model(
    data: Mapping[str, Any],
    task_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        return {"url": data.get("url"), "cookies": [], "count": 0}
    redacted = [{**item, "value": REDACTED} if isinstance(item, dict) else item for item in cookies]
    return redact_task_inputs(
        {"url": data.get("url"), "cookies": redacted, "count": len(redacted)},
        task_inputs,
    )


def _safe_storage_for_model(
    data: Mapping[str, Any],
    task_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(data)
    if payload.get("mode") == "value" and "value" in payload:
        payload["value"] = REDACTED
    return redact_task_inputs(payload, task_inputs)

"""Cookie 与 Web Storage 工具单元测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from witty_browser_auto.agent.storage_tools import (
    StorageToolOutcome,
    effective_allowed_origins,
    execute_storage_tool,
)
from witty_browser_auto.domain.models import ExecutionScope, ModelToolCall, TaskSpec
from witty_browser_auto.security.redaction import REDACTED


class _StorageDriver:
    def __init__(self) -> None:
        self.cookies: list[dict[str, Any]] = []
        self.storage: dict[str, str] = {}
        self.page_url = "https://shop.example/orders"

    async def current_page_url(self) -> str:
        return self.page_url

    async def read_cookies(
        self,
        url: str,
        *,
        names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        assert url == self.page_url
        if names is None:
            return list(self.cookies)
        return [item for item in self.cookies if item["name"] in names]

    async def set_cookie(
        self,
        *,
        name: str,
        value: str,
        url: str,
        path: str = "/",
        domain: str | None = None,
        http_only: bool = False,
        secure: bool = False,
        expires: float | None = None,
    ) -> dict[str, Any]:
        self.cookies.append({"name": name, "value": value})
        return {
            "name": name,
            "url": url,
            "path": path,
            "domain": domain,
            "httpOnly": http_only,
            "secure": secure,
            "expires": expires,
        }

    async def read_web_storage(
        self,
        *,
        storage_kind: str,
        key: str | None = None,
        frame_id: str | None = None,
    ) -> dict[str, Any]:
        assert storage_kind == "local"
        assert frame_id is None
        if key is None:
            return {
                "storage_kind": "local",
                "mode": "keys",
                "keys": list(self.storage),
                "truncated": False,
            }
        return {
            "storage_kind": "local",
            "mode": "value",
            "key": key,
            "value": self.storage.get(key),
            "found": key in self.storage,
        }

    async def write_web_storage(
        self,
        *,
        storage_kind: str,
        key: str,
        value: str | None = None,
        frame_id: str | None = None,
        remove: bool = False,
    ) -> dict[str, Any]:
        assert storage_kind == "local"
        if remove:
            self.storage.pop(key, None)
            return {"storage_kind": "local", "key": key, "action": "removed"}
        assert value is not None
        self.storage[key] = value
        return {"storage_kind": "local", "key": key, "action": "set"}


def _task(**inputs: str) -> TaskSpec:
    return TaskSpec(
        task_id="storage-unit",
        goal="读写存储",
        start_url="https://shop.example/",
        scope=ExecutionScope(project_id="p", allowed_origins=("https://shop.example",)),
        inputs=inputs,
    )


def test_read_cookies_redacts_values_for_model() -> None:
    async def scenario() -> None:
        driver = _StorageDriver()
        driver.cookies = [
            {"name": "sid", "value": "secret-token", "domain": "shop.example", "path": "/"}
        ]
        outcome = await execute_storage_tool(
            ModelToolCall("c1", "read_cookies", {}),
            driver,
            task=_task(),
            task_inputs={},
        )
        assert outcome.success
        assert outcome.data["cookies"][0]["value"] == "secret-token"
        assert outcome.model_data["cookies"][0]["value"] == REDACTED

    asyncio.run(scenario())


def test_set_cookie_resolves_value_input_key() -> None:
    async def scenario() -> None:
        driver = _StorageDriver()
        outcome = await execute_storage_tool(
            ModelToolCall(
                "c2",
                "set_cookie",
                {"name": "sid", "value_input_key": "session"},
            ),
            driver,
            task=_task(session="from-input"),
            task_inputs={"session": "from-input"},
        )
        assert isinstance(outcome, StorageToolOutcome)
        assert outcome.success
        assert driver.cookies[0]["value"] == "from-input"

    asyncio.run(scenario())


def test_set_cookie_rejects_both_value_sources() -> None:
    async def scenario() -> None:
        driver = _StorageDriver()
        with pytest.raises(ValueError, match="二选一"):
            await execute_storage_tool(
                ModelToolCall(
                    "c3",
                    "set_cookie",
                    {"name": "sid", "value": "a", "value_input_key": "session"},
                ),
                driver,
                task=_task(),
                task_inputs={"session": "b"},
            )

    asyncio.run(scenario())


def test_write_web_storage_remove_does_not_need_value() -> None:
    async def scenario() -> None:
        driver = _StorageDriver()
        driver.storage["draft"] = "old"
        outcome = await execute_storage_tool(
            ModelToolCall(
                "c4",
                "write_web_storage",
                {"storage_kind": "local", "key": "draft", "remove": True},
            ),
            driver,
            task=_task(),
            task_inputs={},
        )
        assert outcome.success
        assert "draft" not in driver.storage

    asyncio.run(scenario())


def test_storage_rejects_out_of_scope_url() -> None:
    async def scenario() -> None:
        driver = _StorageDriver()
        with pytest.raises(Exception, match="不在任务授权范围"):
            await execute_storage_tool(
                ModelToolCall(
                    "c5",
                    "set_cookie",
                    {"name": "sid", "value": "x", "url": "https://evil.example/"},
                ),
                driver,
                task=_task(),
                task_inputs={},
            )

    asyncio.run(scenario())


def test_effective_allowed_origins_falls_back_to_start_url() -> None:
    task = TaskSpec(
        task_id="t",
        goal="g",
        start_url="https://shop.example/home",
        scope=ExecutionScope(project_id="p"),
    )
    assert effective_allowed_origins(task) == ("https://shop.example",)

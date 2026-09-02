from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from witty_browser_auto.agent.tools import ToolExecutor
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionReceipt,
    CandidateTarget,
    DragRiskClass,
    DriverCapabilities,
    ExecutionScope,
    LocatorRecipe,
    ModelToolCall,
    Observation,
    TaskSpec,
    VerificationResult,
)
from witty_browser_auto.security.redaction import TASK_INPUT_REDACTED

TAB_TOOL_NAMES = {"list_tabs", "open_tab", "switch_tab", "close_tab"}


class TabDriver:
    capabilities = DriverCapabilities(dom=True, accessibility=True, network=True)

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root
        self.current_target_id = "tab-current"
        self.switch_calls: list[str] = []
        self.close_calls: list[str] = []
        self.open_calls: list[str] = []

    async def start(self) -> None:
        return None

    async def open(self, url: str) -> str:
        return "surface"

    async def observe(self, *, force: bool = False) -> Observation:
        return _observation()

    async def execute(self, command: ActionCommand) -> ActionReceipt:
        return ActionReceipt(command.action_id, True, True, "已执行", 1.0)

    async def verify(self, condition: object) -> VerificationResult:
        return VerificationResult(True, "ok")

    async def capture_evidence(self, label: str) -> Path:
        return self.artifact_root / f"{label}.png"

    async def close(self) -> None:
        return None

    async def list_tabs(self) -> list[dict[str, Any]]:
        return [
            {
                "target_id": "tab-current",
                "url": "https://example.com/list",
                "title": "列表页 account-value",
                "is_current": True,
                "owned_by_task": True,
            },
            {
                "target_id": "tab-other",
                "url": "https://example.com/detail",
                "title": "详情页",
                "is_current": False,
                "owned_by_task": True,
            },
        ]

    async def open_tab(self, url: str) -> dict[str, Any]:
        self.open_calls.append(url)
        self.current_target_id = "tab-new"
        return {"target_id": "tab-new", "opened": True, "url": url}

    async def switch_tab(self, target_id: str) -> dict[str, Any]:
        self.switch_calls.append(target_id)
        if target_id == self.current_target_id:
            return {"target_id": target_id, "switched": False, "already_current": True}
        self.current_target_id = target_id
        return {"target_id": target_id, "switched": True, "borrowed": False}

    async def close_tab(self, target_id: str) -> dict[str, Any]:
        self.close_calls.append(target_id)
        was_current = target_id == self.current_target_id
        result: dict[str, Any] = {
            "target_id": target_id,
            "closed": True,
            "was_current": was_current,
        }
        if was_current:
            self.current_target_id = "tab-fallback"
            result["switched_to"] = "tab-fallback"
        return result


class PlainDriver:
    """没有标签页管理能力的最小驱动。"""

    capabilities = DriverCapabilities(dom=True, accessibility=True, network=True)

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root

    async def start(self) -> None:
        return None

    async def open(self, url: str) -> str:
        return "surface"

    async def observe(self, *, force: bool = False) -> Observation:
        return _observation()

    async def execute(self, command: ActionCommand) -> ActionReceipt:
        return ActionReceipt(command.action_id, True, True, "已执行", 1.0)

    async def verify(self, condition: object) -> VerificationResult:
        return VerificationResult(True, "ok")

    async def capture_evidence(self, label: str) -> Path:
        return self.artifact_root / f"{label}.png"

    async def close(self) -> None:
        return None


def _observation() -> Observation:
    return Observation(
        surface_id="surface",
        url="https://example.com/list",
        title="列表页",
        version=1,
        fingerprint="current",
        summary="任务列表",
        candidates=(
            CandidateTarget(
                "next-page",
                "button",
                "下一页",
                "",
                0.99,
                ("测试",),
                LocatorRecipe("test", role="button", name="下一页"),
                drag_risk=DragRiskClass.BUSINESS,
            ),
        ),
    )


def _task() -> TaskSpec:
    return TaskSpec(
        "tabs",
        "跨标签页采集",
        "https://example.com/list",
        ExecutionScope("project"),
        inputs={"account": "account-value"},
    )


def test_tab_capability_detection(tmp_path: Path) -> None:
    task = _task()
    with_tabs = ToolExecutor(TabDriver(tmp_path), task)  # type: ignore[arg-type]
    without_tabs = ToolExecutor(PlainDriver(tmp_path), task)  # type: ignore[arg-type]

    assert with_tabs.tab_management_available is True
    assert without_tabs.tab_management_available is False


def test_list_tabs_is_read_only_and_redacts_task_inputs(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor = ToolExecutor(TabDriver(tmp_path), _task())  # type: ignore[arg-type]
        result = await executor.execute(
            ModelToolCall("list-call", "list_tabs", {}),
            _observation(),
        )

        assert result.success is True
        assert result.counts_as_action is False
        assert result.idempotent is True
        assert result.data["tab_count"] == 2
        titles = [tab["title"] for tab in result.data["tabs"]]
        assert any(TASK_INPUT_REDACTED in title for title in titles)
        assert "account-value" not in str(result.data)

    asyncio.run(scenario())


def test_switch_tab_invalidates_page_bound_inspections(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = TabDriver(tmp_path)
        executor = ToolExecutor(driver, _task())  # type: ignore[arg-type]
        executor.network_inspection = {"candidates": []}
        executor.network_data_inspected = True
        executor.collection_inspection = {"candidates": []}
        executor.collection_inspected = True

        result = await executor.execute(
            ModelToolCall("switch-call", "switch_tab", {"target_id": "tab-other"}),
            _observation(),
        )

        assert result.success is True
        assert result.counts_as_action is True
        assert driver.switch_calls == ["tab-other"]
        assert executor.network_inspection is None
        assert executor.network_data_inspected is False
        assert executor.collection_inspection is None
        assert executor.collection_inspected is False

    asyncio.run(scenario())


def test_switch_tab_to_current_target_keeps_inspections(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = TabDriver(tmp_path)
        executor = ToolExecutor(driver, _task())  # type: ignore[arg-type]
        executor.collection_inspection = {"candidates": []}
        executor.collection_inspected = True

        result = await executor.execute(
            ModelToolCall("noop-call", "switch_tab", {"target_id": "tab-current"}),
            _observation(),
        )

        assert result.success is True
        assert result.counts_as_action is False
        assert executor.collection_inspected is True

    asyncio.run(scenario())


def test_close_current_tab_invalidates_and_reports_fallback(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = TabDriver(tmp_path)
        executor = ToolExecutor(driver, _task())  # type: ignore[arg-type]
        executor.collection_inspected = True
        executor.collection_inspection = {"candidates": []}

        result = await executor.execute(
            ModelToolCall("close-call", "close_tab", {"target_id": "tab-current"}),
            _observation(),
        )

        assert result.success is True
        assert result.idempotent is False
        assert result.data["switched_to"] == "tab-fallback"
        assert executor.collection_inspected is False

    asyncio.run(scenario())


def test_close_background_tab_keeps_current_page_observations(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = TabDriver(tmp_path)
        executor = ToolExecutor(driver, _task())  # type: ignore[arg-type]
        executor.collection_inspected = True
        executor.collection_inspection = {"candidates": []}

        result = await executor.execute(
            ModelToolCall("close-call", "close_tab", {"target_id": "tab-other"}),
            _observation(),
        )

        assert result.success is True
        assert result.data["was_current"] is False
        assert executor.collection_inspected is True

    asyncio.run(scenario())


def test_open_tab_invalidates_page_bound_inspections(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = TabDriver(tmp_path)
        executor = ToolExecutor(driver, _task())  # type: ignore[arg-type]
        executor.collection_inspection = {"candidates": []}
        executor.collection_inspected = True

        result = await executor.execute(
            ModelToolCall("open-call", "open_tab", {"url": "https://example.com/detail"}),
            _observation(),
        )

        assert result.success is True
        assert result.idempotent is False
        assert result.counts_as_action is True
        assert driver.open_calls == ["https://example.com/detail"]
        assert result.data["target_id"] == "tab-new"
        assert executor.collection_inspected is False

    asyncio.run(scenario())


def test_open_tab_refuses_urls_outside_task_scope(tmp_path: Path) -> None:
    """新页面必须和 navigate 走同一条授权判定，否则它就是绕过导航范围的后门。"""

    async def scenario() -> None:
        driver = TabDriver(tmp_path)
        executor = ToolExecutor(driver, _task())  # type: ignore[arg-type]

        result = await executor.execute(
            ModelToolCall("open-call", "open_tab", {"url": "https://evil.example.net/steal"}),
            _observation(),
        )

        assert result.success is False
        assert driver.open_calls == []

    asyncio.run(scenario())


def test_tab_tool_rejects_invalid_arguments_without_consuming_budget(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = TabDriver(tmp_path)
        executor = ToolExecutor(driver, _task())  # type: ignore[arg-type]

        missing = await executor.execute(
            ModelToolCall("bad-call-1", "switch_tab", {}),
            _observation(),
        )
        unknown = await executor.execute(
            ModelToolCall("bad-call-2", "close_tab", {"target_id": "tab-x", "force": True}),
            _observation(),
        )
        extra_on_list = await executor.execute(
            ModelToolCall("bad-call-3", "list_tabs", {"target_id": "tab-x"}),
            _observation(),
        )

        assert missing.success is False
        assert unknown.success is False
        assert extra_on_list.success is False
        assert driver.switch_calls == []
        assert driver.close_calls == []

    asyncio.run(scenario())


def test_tab_tools_unavailable_driver_fails_gracefully(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor = ToolExecutor(PlainDriver(tmp_path), _task())  # type: ignore[arg-type]
        result = await executor.execute(
            ModelToolCall("no-tab-call", "list_tabs", {}),
            _observation(),
        )

        assert result.success is False
        assert result.counts_as_action is False

    asyncio.run(scenario())

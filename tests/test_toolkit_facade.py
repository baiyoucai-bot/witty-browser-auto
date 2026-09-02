from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionKind,
    ActionReceipt,
    CandidateTarget,
    DragRiskClass,
    DriverCapabilities,
    ExecutionScope,
    ExpectedCondition,
    LocatorRecipe,
    Observation,
    TaskSpec,
    VerificationResult,
)
from witty_browser_auto.toolkit import BrowserToolkit
from witty_browser_auto.toolkit.registry import ToolArgumentError


class RecordingDriver:
    """记录收到的动作命令，用来证明外部调用真的到达了执行层。"""

    capabilities = DriverCapabilities(dom=True, accessibility=True, network=True)

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root
        self.commands: list[ActionCommand] = []
        self.observe_calls = 0
        self.fingerprint = "fp-1"

    async def start(self) -> None:
        return None

    async def open(self, url: str) -> str:
        return "surface"

    async def observe(self, *, force: bool = False) -> Observation:
        self.observe_calls += 1
        return Observation(
            surface_id="surface",
            url="https://example.com/list",
            title="列表页",
            version=self.observe_calls,
            fingerprint=self.fingerprint,
            summary="订单列表",
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

    async def execute(self, command: ActionCommand) -> ActionReceipt:
        self.commands.append(command)
        return ActionReceipt(command.action_id, True, True, "已执行", 1.0)

    async def verify(self, condition: ExpectedCondition) -> VerificationResult:
        return VerificationResult(True, "已校验")

    async def capture_evidence(self, label: str) -> Path:
        return self.artifact_root / f"{label}.png"

    async def close(self) -> None:
        return None


class FormRecordingDriver(RecordingDriver):
    capabilities = DriverCapabilities(dom=True, accessibility=True, forms=True)

    async def fill_fields(self, fields: Any) -> list[dict[str, Any]]:
        return [
            {"index": index, "filled": True, "target_id": getattr(field, "target_id", None)}
            for index, field in enumerate(fields)
        ]


def _task(*, allow_visual_actions: bool = False, read_only: bool = False) -> TaskSpec:
    return TaskSpec(
        "toolkit-task",
        "外部调用浏览器工具",
        "https://example.com/list",
        ExecutionScope("project"),
        allow_visual_actions=allow_visual_actions,
        read_only=read_only,
    )


def _toolkit(tmp_path: Path, *, visual: bool = False) -> tuple[BrowserToolkit, RecordingDriver]:
    driver = RecordingDriver(tmp_path)
    toolkit = BrowserToolkit(
        driver,  # type: ignore[arg-type]
        _task(allow_visual_actions=visual),
        visual_context_available=visual,
    )
    return toolkit, driver


def test_navigate_reaches_execution_layer(tmp_path: Path) -> None:
    toolkit, driver = _toolkit(tmp_path)

    result = asyncio.run(toolkit.navigate("https://example.com/orders"))

    assert result.success, result.message
    assert [command.kind for command in driver.commands] == [ActionKind.NAVIGATE]
    assert driver.commands[0].url == "https://example.com/orders"


def test_call_by_name_matches_convenience_method(tmp_path: Path) -> None:
    toolkit, driver = _toolkit(tmp_path)

    result = asyncio.run(toolkit.call("scroll", amount=320))

    assert result.success, result.message
    assert driver.commands[0].kind is ActionKind.SCROLL


def test_read_only_task_refuses_mutation_before_driver_execute(tmp_path: Path) -> None:
    toolkit = BrowserToolkit(RecordingDriver(tmp_path), _task(read_only=True))

    result = asyncio.run(
        toolkit.call(
            "click",
            target_id="next-page",
            expect_kind="fingerprint_changed",
            expect_value="",
        )
    )

    assert result.success is False
    assert result.failure_kind is not None
    assert result.failure_kind.value == "policy"
    assert result.data == {"read_only": True, "tool": "click"}
    assert toolkit.driver.commands == []


def test_form_actions_are_included_in_exported_script(tmp_path: Path) -> None:
    driver = FormRecordingDriver(tmp_path)
    toolkit = BrowserToolkit(driver, _task())

    result = asyncio.run(
        toolkit.call(
            "fill_form",
            fields=[{"target_id": "next-page", "text": "备注"}],
        )
    )
    exported = asyncio.run(toolkit.export_action_script())

    assert result.success, result.message
    assert exported.success, exported.message
    assert exported.data["step_count"] == 1
    assert '"fill_form"' in exported.data["code"]
    assert '"target_id"' not in exported.data["code"]
    assert '"locator"' in exported.data["code"]


def test_click_uses_observed_target(tmp_path: Path) -> None:
    toolkit, driver = _toolkit(tmp_path)

    result = asyncio.run(
        toolkit.click("next-page", expect_kind="url_contains", expect_value="/list")
    )

    assert result.success, result.message
    assert driver.commands[0].kind is ActionKind.CLICK


def test_click_without_postcondition_defaults_to_page_change(tmp_path: Path) -> None:
    """探索性点击不必编造判据：缺省绑定当前观察指纹，按"页面有变化"校验。"""

    toolkit, driver = _toolkit(tmp_path)

    result = asyncio.run(toolkit.click("next-page"))

    assert result.success, result.message
    command = driver.commands[0]
    assert command.kind is ActionKind.CLICK
    assert command.expected is not None
    assert command.expected.kind == "fingerprint_changed"
    assert command.expected.value == driver.fingerprint


def test_explicit_postcondition_still_wins_over_the_default(tmp_path: Path) -> None:
    toolkit, driver = _toolkit(tmp_path)

    asyncio.run(toolkit.hover("next-page", expect_kind="text_contains", expect_value="全部分类"))
    asyncio.run(toolkit.press_key("escape"))

    assert driver.commands[0].expected is not None
    assert driver.commands[0].expected.kind == "text_contains"
    assert driver.commands[1].expected is not None
    assert driver.commands[1].expected.kind == "fingerprint_changed"


def test_half_specified_postcondition_is_rejected(tmp_path: Path) -> None:
    """只给 expect_kind 不给 expect_value 是调用方的错误，不能被缺省逻辑悄悄补齐。"""

    toolkit, driver = _toolkit(tmp_path)

    result = asyncio.run(toolkit.click("next-page", expect_kind="url_contains"))

    assert result.success is False
    assert "同时提供" in result.message
    assert driver.commands == []


def test_input_text_accepts_a_non_sensitive_literal(tmp_path: Path) -> None:
    driver = RecordingDriver(tmp_path)
    toolkit = BrowserToolkit(
        driver,  # type: ignore[arg-type]
        TaskSpec(
            "toolkit-task",
            "搜索",
            "https://example.com/list",
            ExecutionScope("project"),
            inputs={"password": "hunter2"},
        ),
    )

    typed = asyncio.run(toolkit.input_text("next-page", text="iPhone 15"))
    assert typed.success, typed.message
    assert driver.commands[0].kind is ActionKind.INPUT_TEXT
    assert driver.commands[0].value == "iPhone 15"

    # 字面量与任务输入的值撞车，说明模型把凭据抄进了参数——必须拒绝。
    leaked = asyncio.run(toolkit.input_text("next-page", text="hunter2"))
    assert leaked.success is False
    assert leaked.failure_kind is not None and leaked.failure_kind.value == "policy"
    assert len(driver.commands) == 1

    both = asyncio.run(toolkit.input_text("next-page", input_key="password", text="x"))
    assert both.success is False
    assert "只能提供一个" in both.message

    neither = asyncio.run(toolkit.input_text("next-page"))
    assert neither.success is False
    assert "input_key" in neither.message and "text" in neither.message


def test_observation_fingerprint_is_bound_by_code(tmp_path: Path) -> None:
    """指纹属于执行期状态，调用方不应被要求手抄。"""

    toolkit, driver = _toolkit(tmp_path, visual=True)

    result = asyncio.run(
        toolkit.visual_click(
            screenshot_fingerprint="shot-1",
            x_ratio=0.5,
            y_ratio=0.5,
            visual_confidence=0.9,
            expect_kind="url_contains",
            expect_value="/orders",
        )
    )

    assert result.success, result.message
    assert driver.commands[0].observation_fingerprint == driver.fingerprint


def test_arguments_are_validated_before_touching_browser(tmp_path: Path) -> None:
    toolkit, driver = _toolkit(tmp_path)

    with pytest.raises(ToolArgumentError, match="必须是字符串"):
        asyncio.run(toolkit.call("navigate", url=42))

    assert driver.commands == []


def test_unknown_parameter_is_rejected_locally(tmp_path: Path) -> None:
    toolkit, driver = _toolkit(tmp_path)

    with pytest.raises(ToolArgumentError, match="未知参数"):
        asyncio.run(toolkit.call("scroll", amount=10, behaviour="smooth"))

    assert driver.commands == []


def test_engine_only_tools_are_refused_with_reason(tmp_path: Path) -> None:
    toolkit, _ = _toolkit(tmp_path)

    for name, arguments in (
        ("finish", {"summary": "完成"}),
        ("block", {"reason": "需要凭据"}),
        ("ask_user", {}),
        ("wait_until", {}),
    ):
        with pytest.raises(ToolArgumentError, match="不开放外部直接调用"):
            asyncio.run(toolkit.call(name, arguments))


def test_unknown_tool_is_refused(tmp_path: Path) -> None:
    toolkit, _ = _toolkit(tmp_path)

    with pytest.raises(ToolArgumentError, match="未注册的工具"):
        asyncio.run(toolkit.call("open_devtools"))


def test_observation_is_refreshed_after_page_action(tmp_path: Path) -> None:
    """页面动作后旧观察失效，新观察随结果一起返回，下一步元素操作直接基于它。"""

    toolkit, driver = _toolkit(tmp_path)

    async def scenario() -> None:
        first = await toolkit.observe()
        assert driver.observe_calls == 1
        navigated = await toolkit.navigate("https://example.com/orders")
        # 动作后立刻重新观察：结果自带新观察，门面缓存也换成了它。
        assert driver.observe_calls == 2
        assert navigated.observation is not None
        assert navigated.observation.version == 2
        assert navigated.observation is not first
        assert toolkit.observation is navigated.observation
        # 下一步动作不再需要额外 observe。
        clicked = await toolkit.click("next-page", expect_kind="url_contains", expect_value="/list")
        assert driver.observe_calls == 3
        assert clicked.observation is not None
        assert clicked.observation.version == 3

    asyncio.run(scenario())


def test_post_action_observation_can_be_disabled(tmp_path: Path) -> None:
    """关闭自动刷新后退回惰性观察：动作后缓存为空，下一次调用时才观察。"""

    driver = RecordingDriver(tmp_path)
    toolkit = BrowserToolkit(
        driver,  # type: ignore[arg-type]
        _task(),
        refresh_observation_after_action=False,
    )

    async def scenario() -> None:
        await toolkit.observe()
        result = await toolkit.navigate("https://example.com/orders")
        assert result.observation is None
        assert toolkit.observation is None
        assert driver.observe_calls == 1
        await toolkit.click("next-page", expect_kind="url_contains", expect_value="/list")
        assert driver.observe_calls == 2

    asyncio.run(scenario())


def test_post_action_observation_failure_does_not_mask_action_result(tmp_path: Path) -> None:
    """页面在动作后被关闭之类的观察失败，只丢快照，不改动作结论。"""

    class ClosingDriver(RecordingDriver):
        async def observe(self, *, force: bool = False) -> Observation:
            if self.commands:
                raise RuntimeError("页面已关闭")
            return await super().observe(force=force)

    driver = ClosingDriver(tmp_path)
    toolkit = BrowserToolkit(driver, _task())  # type: ignore[arg-type]

    result = asyncio.run(toolkit.navigate("https://example.com/orders"))

    assert result.success, result.message
    assert result.observation is None
    assert toolkit.observation is None


def test_successful_wait_for_condition_refreshes_observation(tmp_path: Path) -> None:
    """等待成功意味着页面已经变了，旧候选不能再用。"""

    class WaitingDriver(FormRecordingDriver):
        async def wait_for(self, condition: ExpectedCondition) -> dict[str, Any]:
            return {"satisfied": True, "waited_seconds": 0.2}

    driver = WaitingDriver(tmp_path)
    toolkit = BrowserToolkit(driver, _task())  # type: ignore[arg-type]

    async def scenario() -> None:
        await toolkit.observe()
        assert driver.observe_calls == 1
        result = await toolkit.call(
            "wait_for_condition", expect_kind="text_contains", expect_value="导出完成"
        )
        assert result.success, result.message
        assert result.counts_as_action is False
        assert result.observation is not None
        assert driver.observe_calls == 2

    asyncio.run(scenario())


def test_read_only_tools_keep_current_observation(tmp_path: Path) -> None:
    toolkit, driver = _toolkit(tmp_path)

    async def scenario() -> None:
        await toolkit.observe()
        await toolkit.screenshot("证据")
        assert toolkit.observation is not None
        assert driver.observe_calls == 1

    asyncio.run(scenario())


def test_tool_names_exclude_engine_only_tools(tmp_path: Path) -> None:
    toolkit, _ = _toolkit(tmp_path)

    names = set(toolkit.tool_names())

    assert {"click", "navigate", "list_tabs", "inspect_network_data"} <= names
    assert names.isdisjoint({"finish", "ask_user", "block", "wait_until"})


def test_describe_tools_can_be_filtered_by_category(tmp_path: Path) -> None:
    toolkit, _ = _toolkit(tmp_path)

    described: tuple[dict[str, Any], ...] = toolkit.describe_tools(category="tab")

    assert {item["name"] for item in described} == {
        "list_tabs",
        "open_tab",
        "switch_tab",
        "close_tab",
    }
    assert all(item["returns"] for item in described)


def test_optional_arguments_are_dropped_when_none(tmp_path: Path) -> None:
    """便捷方法用 None 表示未提供，不能把 null 传进执行层校验。"""

    toolkit, driver = _toolkit(tmp_path)

    result = asyncio.run(
        toolkit.select_locator(
            {"strategy": "css", "value": "#city"},
            expect_kind="url_contains",
            expect_value="/list",
            value="北京",
        )
    )

    assert result.success, result.message
    assert driver.commands[0].kind is ActionKind.SELECT

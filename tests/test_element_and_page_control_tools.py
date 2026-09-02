"""元素只读读取、功能键派发与页面历史导航的确定性回归。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from witty_browser_auto.agent import element_tools
from witty_browser_auto.browser.keyboard import (
    dispatch_key_press,
    resolve_key,
    supported_key_names,
    supported_modifier_names,
)
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
    ModelToolCall,
    Observation,
    TaskSpec,
    VerificationResult,
)
from witty_browser_auto.toolkit import BrowserToolkit
from witty_browser_auto.toolkit.registry import ToolArgumentError


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, dict(params or {})))
        return {}


class ElementDriver:
    """记录读取参数并返回固定元素状态的替身驱动。"""

    capabilities = DriverCapabilities(dom=True, accessibility=True)

    def __init__(self, artifact_root: Path, state: dict[str, Any] | None = None) -> None:
        self.artifact_root = artifact_root
        self.commands: list[ActionCommand] = []
        self.inspect_calls: list[dict[str, Any]] = []
        self.observe_calls = 0
        self.fingerprint = "fp-1"
        self.state = state or {"tag": "input", "role": "textbox", "text": "订单号"}
        self.candidates: tuple[CandidateTarget, ...] = (
            CandidateTarget(
                "search-input",
                "textbox",
                "订单号",
                "",
                0.99,
                ("测试",),
                LocatorRecipe("test", role="textbox", name="订单号"),
                drag_risk=DragRiskClass.BUSINESS,
            ),
        )

    async def start(self) -> None:
        return None

    async def open(self, url: str) -> str:
        return "surface"

    async def observe(self, *, force: bool = False) -> Observation:
        self.observe_calls += 1
        return Observation(
            surface_id="surface",
            url="https://example.com/orders",
            title="订单查询",
            version=self.observe_calls,
            fingerprint=self.fingerprint,
            summary="订单查询页",
            candidates=self.candidates,
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

    async def inspect_element(
        self,
        *,
        target_id: str | None = None,
        locator: LocatorRecipe | None = None,
        max_text_length: int = 2000,
        include_html: bool = False,
    ) -> dict[str, Any]:
        self.inspect_calls.append(
            {
                "target_id": target_id,
                "locator": locator,
                "max_text_length": max_text_length,
                "include_html": include_html,
            }
        )
        return dict(self.state)


def _task(goal: str = "读取订单状态并按回车查询") -> TaskSpec:
    return TaskSpec(
        "element-task",
        goal,
        "https://example.com/orders",
        ExecutionScope("project"),
        inputs={"order_number": "A-9527"},
    )


def _toolkit(
    tmp_path: Path, state: dict[str, Any] | None = None
) -> tuple[BrowserToolkit, ElementDriver]:
    driver = ElementDriver(tmp_path, state)
    toolkit = BrowserToolkit(driver, _task())  # type: ignore[arg-type]
    return toolkit, driver


# ---- 键盘编译 ----


def test_named_keys_compile_to_native_key_events() -> None:
    resolved = resolve_key("enter", ())

    assert resolved["key"] == "Enter"
    assert resolved["code"] == "Enter"
    assert resolved["windowsVirtualKeyCode"] == 13
    assert resolved["text"] == "\r"
    assert resolved["modifiers"] == 0


def test_modifier_mask_follows_chromium_convention() -> None:
    resolved = resolve_key("a", ("control", "shift"))

    assert resolved["code"] == "KeyA"
    assert resolved["windowsVirtualKeyCode"] == 65
    assert resolved["modifiers"] == 2 | 8
    # 控制类修饰键改变按键语义，不再提交字符文本。
    assert resolved["text"] == ""


def test_bare_letter_key_is_rejected_in_favour_of_input_text() -> None:
    with pytest.raises(ValueError, match="必须配合修饰键"):
        resolve_key("a", ())


def test_unknown_key_and_modifier_are_rejected() -> None:
    with pytest.raises(ValueError, match="不支持的按键名"):
        resolve_key("any_key", ())
    with pytest.raises(ValueError, match="不支持的修饰键"):
        resolve_key("enter", ("hyper",))
    with pytest.raises(ValueError, match="修饰键不能重复"):
        resolve_key("enter", ("shift", "shift"))


def test_supported_names_stay_in_sync_with_contract() -> None:
    assert "enter" in supported_key_names()
    assert "f12" in supported_key_names()
    assert set(supported_modifier_names()) == {"alt", "control", "meta", "shift"}


def test_key_press_dispatches_paired_events_without_business_content() -> None:
    session = FakeSession()

    audit = asyncio.run(dispatch_key_press(session, resolve_key("escape", ()), repeat=2))

    methods = [method for method, _ in session.calls]
    assert methods == ["Input.dispatchKeyEvent"] * 4
    assert [params["type"] for _, params in session.calls] == [
        "rawKeyDown",
        "keyUp",
        "rawKeyDown",
        "keyUp",
    ]
    assert audit == {
        "key": "Escape",
        "code": "Escape",
        "modifier_mask": 0,
        "repeat": 2,
        "submits_text": False,
    }


def test_key_press_rejects_out_of_range_repeat() -> None:
    session = FakeSession()

    with pytest.raises(ValueError, match="1 到 20"):
        asyncio.run(dispatch_key_press(session, resolve_key("tab", ()), repeat=0))

    assert session.calls == []


# ---- 命令编译 ----


def _call(name: str, arguments: dict[str, Any]) -> ModelToolCall:
    return ModelToolCall(call_id="c-1", name=name, arguments=arguments)


def test_press_key_requires_business_post_condition() -> None:
    with pytest.raises(ValueError, match="必须提供业务后置条件"):
        element_tools.build_page_control_command(_call("press_key", {"key": "enter"}), "a-1", None)


def test_press_key_command_carries_compiled_specification() -> None:
    command = element_tools.build_page_control_command(
        _call("press_key", {"key": "enter", "repeat": 3}),
        "a-1",
        ExpectedCondition("url_contains", "/result", 5),
    )

    assert command.kind is ActionKind.PRESS_KEY
    assert command.idempotent is False
    assert json.loads(command.value or "{}")["repeat"] == 3


def test_press_key_rejects_both_target_and_locator() -> None:
    with pytest.raises(ValueError, match="只能提供 target_id 或 locator"):
        element_tools.build_page_control_command(
            _call(
                "press_key",
                {
                    "key": "enter",
                    "target_id": "t-1",
                    "locator": {"strategy": "css", "value": "#q"},
                },
            ),
            "a-1",
            ExpectedCondition("url_contains", "/result", 5),
        )


def test_navigate_history_only_accepts_declared_actions() -> None:
    command = element_tools.build_page_control_command(
        _call("navigate_history", {"action": "back"}),
        "a-1",
        ExpectedCondition("url_contains", "/orders", 5),
    )
    assert command.kind is ActionKind.NAVIGATE_HISTORY
    assert command.value == "back"

    with pytest.raises(ValueError, match="back、forward 或 reload"):
        element_tools.build_page_control_command(
            _call("navigate_history", {"action": "replay"}),
            "a-1",
            ExpectedCondition("url_contains", "/orders", 5),
        )


# ---- 元素读取 ----


def test_read_element_requires_exactly_one_target(tmp_path: Path) -> None:
    driver = ElementDriver(tmp_path)

    with pytest.raises(ValueError, match="只能提供 target_id 或 locator"):
        asyncio.run(
            element_tools.execute_element_read_tool(
                _call("read_element", {}), driver, task_inputs={}
            )
        )
    with pytest.raises(ValueError, match="只能提供 target_id 或 locator"):
        asyncio.run(
            element_tools.execute_element_read_tool(
                _call(
                    "read_element",
                    {"target_id": "t-1", "locator": {"strategy": "css", "value": "#q"}},
                ),
                driver,
                task_inputs={},
            )
        )


def test_read_element_redacts_task_inputs_and_url_attributes(tmp_path: Path) -> None:
    driver = ElementDriver(
        tmp_path,
        {
            "tag": "a",
            "text": "订单 A-9527 详情",
            "attributes": {
                "href": "https://example.com/detail?token=secret-token&id=7",
                "id": "detail-link",
            },
        },
    )

    outcome = asyncio.run(
        element_tools.execute_element_read_tool(
            _call("read_element", {"target_id": "t-1"}),
            driver,
            task_inputs={"order_number": "A-9527"},
        )
    )

    assert outcome.success
    assert "A-9527" not in json.dumps(outcome.data, ensure_ascii=False)
    assert "secret-token" not in outcome.data["attributes"]["href"]
    assert outcome.data["attributes"]["id"] == "detail-link"


def test_read_element_reports_missing_driver_capability(tmp_path: Path) -> None:
    class PlainDriver(ElementDriver):
        inspect_element = None  # type: ignore[assignment]

    outcome = asyncio.run(
        element_tools.execute_element_read_tool(
            _call("read_element", {"target_id": "t-1"}),
            PlainDriver(tmp_path),
            task_inputs={},
        )
    )

    assert outcome.success is False
    assert "没有元素读取能力" in outcome.message


# ---- 外部调用入口 ----


def test_facade_press_key_binds_current_fingerprint(tmp_path: Path) -> None:
    """页面变化条件绑定的是执行期指纹，调用方不需要手抄。"""

    toolkit, driver = _toolkit(tmp_path)

    result = asyncio.run(toolkit.press_key("enter"))

    assert result.success, result.message
    command = driver.commands[0]
    assert command.kind is ActionKind.PRESS_KEY
    assert command.expected is not None
    assert command.expected.kind == "fingerprint_changed"
    assert command.expected.value == driver.fingerprint


def test_facade_history_helpers_reach_execution_layer(tmp_path: Path) -> None:
    toolkit, driver = _toolkit(tmp_path)

    async def scenario() -> None:
        assert (await toolkit.go_back()).success
        assert (await toolkit.go_forward()).success
        assert (await toolkit.reload()).success

    asyncio.run(scenario())

    assert [command.kind for command in driver.commands] == [ActionKind.NAVIGATE_HISTORY] * 3
    assert [command.value for command in driver.commands] == ["back", "forward", "reload"]


def test_facade_read_element_does_not_invalidate_observation(tmp_path: Path) -> None:
    toolkit, driver = _toolkit(tmp_path)

    async def scenario() -> None:
        await toolkit.observe()
        result = await toolkit.read_element("search-input", max_text_length=500)
        assert result.success, result.message
        assert result.counts_as_action is False
        assert toolkit.observation is not None
        assert driver.observe_calls == 1

    asyncio.run(scenario())

    assert driver.inspect_calls[0]["target_id"] == "search-input"
    assert driver.inspect_calls[0]["max_text_length"] == 500


def test_facade_read_element_rejects_unknown_parameter(tmp_path: Path) -> None:
    toolkit, driver = _toolkit(tmp_path)

    with pytest.raises(ToolArgumentError, match="未知参数"):
        asyncio.run(toolkit.call("read_element", {"target_id": "t-1", "depth": 2}))

    assert driver.inspect_calls == []

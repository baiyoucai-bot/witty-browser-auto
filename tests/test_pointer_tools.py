"""悬停、右键、双击、元素截图与新建标签页的确定性回归。

这些行为都只能由真实 Chrome 证伪最终效果，但事件序列、坐标换算和授权判定必须在
单测里锁死：派发顺序错了页面收到的就是另一种交互，clip 少加滚动偏移就会截错区域。
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest

from witty_browser_auto.agent import element_tools
from witty_browser_auto.browser.driver import element_screenshot_clip
from witty_browser_auto.browser.mouse import (
    dispatch_click,
    dispatch_hover,
    resolve_pointer,
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

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n-fake-element-shot").decode()


class FakeSession:
    def __init__(self, *, scroll: tuple[float, float] = (0.0, 0.0)) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.scroll = scroll

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, dict(params or {})))
        if method == "Runtime.evaluate":
            return {"result": {"value": [self.scroll[0], self.scroll[1]]}}
        if method == "Page.captureScreenshot":
            return {"data": _PNG}
        return {}

    def events(self) -> list[dict[str, Any]]:
        return [params for method, params in self.calls if method == "Input.dispatchMouseEvent"]


class PointerDriver:
    """记录命令与截图参数的替身驱动。"""

    capabilities = DriverCapabilities(dom=True, accessibility=True)

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root
        self.commands: list[ActionCommand] = []
        self.shot_calls: list[dict[str, Any]] = []
        self.open_calls: list[str] = []
        self.observe_calls = 0

    async def start(self) -> None:
        return None

    async def open(self, url: str) -> str:
        return "surface"

    async def observe(self, *, force: bool = False) -> Observation:
        self.observe_calls += 1
        return Observation(
            surface_id="surface",
            url="https://example.com/files",
            title="文件列表",
            version=self.observe_calls,
            fingerprint="fp-1",
            summary="文件列表页",
            candidates=(
                CandidateTarget(
                    "file-row",
                    "row",
                    "季度报表",
                    "",
                    0.99,
                    ("测试",),
                    LocatorRecipe("test", role="row", name="季度报表"),
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

    async def capture_element_screenshot(
        self,
        *,
        target_id: str | None = None,
        locator: LocatorRecipe | None = None,
        label: str = "element",
        padding: float = 0.0,
    ) -> dict[str, Any]:
        self.shot_calls.append(
            {"target_id": target_id, "locator": locator, "label": label, "padding": padding}
        )
        return {"screenshot_path": str(self.artifact_root / f"{label}.png")}

    async def list_tabs(self) -> list[dict[str, Any]]:
        return []

    async def open_tab(self, url: str) -> dict[str, Any]:
        self.open_calls.append(url)
        return {"target_id": "tab-new", "opened": True, "url": url}

    async def switch_tab(self, target_id: str) -> dict[str, Any]:
        return {"target_id": target_id, "switched": True}

    async def close_tab(self, target_id: str) -> dict[str, Any]:
        return {"target_id": target_id, "closed": True}


def _task() -> TaskSpec:
    return TaskSpec(
        "pointer-task",
        "在文件列表上右键重命名",
        "https://example.com/files",
        ExecutionScope("project"),
    )


def _toolkit(tmp_path: Path) -> tuple[BrowserToolkit, PointerDriver]:
    driver = PointerDriver(tmp_path)
    return BrowserToolkit(driver, _task()), driver  # type: ignore[arg-type]


# ---- 指针事件序列 ----


def test_hover_only_moves_the_pointer_without_pressing() -> None:
    session = FakeSession()

    asyncio.run(dispatch_hover(session, 120, 40))  # type: ignore[arg-type]

    assert [event["type"] for event in session.events()] == ["mouseMoved"]
    assert session.events()[0]["buttons"] == 0


def test_right_click_uses_the_secondary_button_mask() -> None:
    """Chrome 会自己在 mousedown 与 mouseup 之间补 contextmenu，所以不额外派发。"""

    session = FakeSession()

    asyncio.run(dispatch_click(session, 10, 20, button="right"))  # type: ignore[arg-type]

    events = session.events()
    assert [event["type"] for event in events] == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert events[1]["button"] == "right"
    assert events[1]["buttons"] == 2
    assert events[2]["buttons"] == 0
    assert all(method == "Input.dispatchMouseEvent" for method, _ in session.calls)


def test_double_click_sends_two_rounds_with_increasing_click_count() -> None:
    """只发一轮 clickCount=2 虽然也触发 dblclick，但页面收不到第一次 click。"""

    session = FakeSession()

    asyncio.run(dispatch_click(session, 10, 20, click_count=2))  # type: ignore[arg-type]

    events = session.events()
    assert [event["type"] for event in events] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
        "mousePressed",
        "mouseReleased",
    ]
    assert [event["clickCount"] for event in events[1:]] == [1, 1, 2, 2]


def test_middle_click_uses_the_auxiliary_button_mask() -> None:
    session = FakeSession()

    asyncio.run(dispatch_click(session, 10, 20, button="middle"))  # type: ignore[arg-type]

    assert session.events()[1]["buttons"] == 4


def test_unsupported_button_and_click_count_are_rejected_before_dispatch() -> None:
    session = FakeSession()

    with pytest.raises(ValueError, match="不支持的鼠标按键"):
        asyncio.run(dispatch_click(session, 1, 1, button="fourth"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="点击次数"):
        asyncio.run(dispatch_click(session, 1, 1, click_count=9))  # type: ignore[arg-type]

    assert session.events() == []


def test_pointer_options_default_to_a_plain_left_single_click() -> None:
    assert resolve_pointer(None, None) == ("left", 1)
    assert resolve_pointer("right", 2) == ("right", 2)
    with pytest.raises(ValueError, match="button"):
        resolve_pointer("scroll", 1)
    with pytest.raises(ValueError, match="click_count"):
        resolve_pointer("left", True)


# ---- 命令编译 ----


def _call(name: str, arguments: dict[str, Any]) -> ModelToolCall:
    return ModelToolCall(call_id="c-1", name=name, arguments=arguments)


def test_hover_compiles_to_an_idempotent_action_with_a_postcondition() -> None:
    command = element_tools.build_pointer_command(
        _call("hover", {"target_id": "nav-products"}),
        "act-1",
        ExpectedCondition("text_contains", "全部分类"),
    )

    assert command.kind is ActionKind.HOVER
    assert command.target_id == "nav-products"
    # 悬停不按下任何键，不提交业务写操作，因此失败后可以安全重试。
    assert command.idempotent is True


def test_hover_without_a_postcondition_is_rejected() -> None:
    with pytest.raises(ValueError, match="后置条件"):
        element_tools.build_pointer_command(_call("hover", {"target_id": "x"}), "act-1", None)


def test_hover_requires_exactly_one_target() -> None:
    with pytest.raises(ValueError, match="只能提供"):
        element_tools.build_pointer_command(
            _call("hover", {"target_id": "x", "locator": {"strategy": "css", "value": "#y"}}),
            "act-1",
            ExpectedCondition("text_contains", "菜单"),
        )


def test_only_click_may_carry_a_button_or_repeat_count() -> None:
    with pytest.raises(ValueError, match="只有点击动作"):
        ActionCommand("a", ActionKind.HOVER, target_id="x", pointer_button="right")
    with pytest.raises(ValueError, match="只有点击动作"):
        ActionCommand("a", ActionKind.SCROLL, click_count=2)


# ---- 工具层 ----


def test_right_click_and_double_click_reach_the_driver_as_click_variants(tmp_path: Path) -> None:
    async def scenario() -> None:
        toolkit, driver = _toolkit(tmp_path)
        await toolkit.observe()
        await toolkit.right_click("file-row", expect_kind="text_contains", expect_value="重命名")
        await toolkit.observe()
        await toolkit.double_click("file-row", expect_kind="text_contains", expect_value="编辑中")

        right, double = driver.commands
        assert (right.kind, right.pointer_button, right.click_count) == (
            ActionKind.CLICK,
            "right",
            1,
        )
        assert (double.kind, double.pointer_button, double.click_count) == (
            ActionKind.CLICK,
            "left",
            2,
        )
        # 右键与双击都不能当作可重放的幂等动作。
        assert right.idempotent is False
        assert double.idempotent is False

    asyncio.run(scenario())


def test_locator_click_also_accepts_button_and_click_count(tmp_path: Path) -> None:
    async def scenario() -> None:
        toolkit, driver = _toolkit(tmp_path)
        await toolkit.click_locator(
            {"strategy": "css", "value": "#chart"},
            button="right",
            expect_kind="text_contains",
            expect_value="导出图片",
        )

        command = driver.commands[0]
        assert command.pointer_button == "right"
        assert command.locator is not None

    asyncio.run(scenario())


def test_right_click_and_double_click_accept_a_locator_for_roleless_targets(
    tmp_path: Path,
) -> None:
    """表格行、卡片这类右键目标没有语义角色，观察候选里根本不会出现。"""

    async def scenario() -> None:
        toolkit, driver = _toolkit(tmp_path)
        await toolkit.right_click(
            locator={"strategy": "css", "value": "#row"},
            expect_kind="text_contains",
            expect_value="重命名",
        )
        await toolkit.double_click(
            locator={"strategy": "css", "value": "#row"},
            expect_kind="text_contains",
            expect_value="编辑中",
        )

        right, double = driver.commands
        assert (right.pointer_button, right.click_count) == ("right", 1)
        assert (double.pointer_button, double.click_count) == ("left", 2)
        assert right.locator is not None and double.locator is not None
        # 走定位器时不该再要求先观察拿 target_id。
        assert right.target_id is None and double.target_id is None

    asyncio.run(scenario())


def test_pointer_shortcuts_reject_ambiguous_targeting(tmp_path: Path) -> None:
    async def scenario() -> None:
        toolkit, driver = _toolkit(tmp_path)
        await toolkit.observe()

        with pytest.raises(ValueError):
            await toolkit.right_click(expect_kind="text_contains", expect_value="x")
        with pytest.raises(ValueError):
            await toolkit.double_click(
                "file-row",
                locator={"strategy": "css", "value": "#row"},
                expect_kind="text_contains",
                expect_value="x",
            )

        assert driver.commands == []

    asyncio.run(scenario())


def test_invalid_button_is_rejected_before_touching_the_browser(tmp_path: Path) -> None:
    async def scenario() -> None:
        toolkit, driver = _toolkit(tmp_path)
        await toolkit.observe()

        with pytest.raises(ToolArgumentError):
            await toolkit.click(
                "file-row",
                expect_kind="text_contains",
                expect_value="x",
                button="fourth",
            )
        with pytest.raises(ToolArgumentError):
            await toolkit.click(
                "file-row", expect_kind="text_contains", expect_value="x", click_count=9
            )

        assert driver.commands == []

    asyncio.run(scenario())


def test_element_screenshot_is_read_only_and_keeps_the_observation(tmp_path: Path) -> None:
    async def scenario() -> None:
        toolkit, driver = _toolkit(tmp_path)
        observation = await toolkit.observe()

        result = await toolkit.capture_element_screenshot("file-row", label="报表行", padding=8)

        assert result.success is True
        assert result.counts_as_action is False
        assert driver.shot_calls == [
            {"target_id": "file-row", "locator": None, "label": "报表行", "padding": 8.0}
        ]
        # 只读工具不作废观察，后续动作不需要重新观察。
        assert toolkit.observation is observation

    asyncio.run(scenario())


def test_element_screenshot_requires_exactly_one_target(tmp_path: Path) -> None:
    async def scenario() -> None:
        toolkit, driver = _toolkit(tmp_path)

        result = await toolkit.capture_element_screenshot()

        assert result.success is False
        assert driver.shot_calls == []

    asyncio.run(scenario())


# ---- 元素截图坐标换算 ----


def test_element_clip_adds_page_scroll_to_the_viewport_box() -> None:
    """captureScreenshot 的 clip 是页面坐标，而工具统一用视口坐标，必须补滚动偏移。"""

    box = {"x": 40.0, "y": 50.0, "width": 120.0, "height": 80.0}

    unscrolled = element_screenshot_clip(box, (0.0, 0.0), 0.0)
    scrolled = element_screenshot_clip(box, (0.0, 1150.0), 0.0)

    assert (unscrolled["x"], unscrolled["y"]) == (40.0, 50.0)
    assert (scrolled["x"], scrolled["y"]) == (40.0, 1200.0)
    assert scrolled["width"] == 120.0


def test_element_clip_expands_by_padding_without_going_negative() -> None:
    box = {"x": 4.0, "y": 6.0, "width": 100.0, "height": 40.0}

    clip = element_screenshot_clip(box, (0.0, 0.0), 12.0)

    assert (clip["x"], clip["y"]) == (0.0, 0.0)
    assert (clip["width"], clip["height"]) == (124.0, 64.0)

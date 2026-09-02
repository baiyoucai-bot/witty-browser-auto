from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import stat
from pathlib import Path
from typing import Any

from witty_browser_auto.browser.driver import CdpAutomationDriver
from witty_browser_auto.browser.mouse import dispatch_click, dispatch_drag
from witty_browser_auto.config import BrowserConfig
from witty_browser_auto.domain.errors import CdpCommandError
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionKind,
    BoundingBox,
    CandidateTarget,
    DragPoint,
    DragRiskClass,
    LocatorRecipe,
    Observation,
    VisualDragPoint,
)


class StubSession:
    target_id = "surface"
    observation_version = 1

    def __init__(self, *, fail_call: int | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_call = fail_call

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = params or {}
        self.calls.append((method, payload))
        if self.fail_call == len(self.calls):
            raise CdpCommandError("模拟拖拽中断", method=method, error_code=-32000)
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 1000, "clientHeight": 500}}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"stable-frame").decode("ascii")}
        return {}


class DragDriver(CdpAutomationDriver):
    async def _resolve_target(
        self,
        target_id: str,
    ) -> tuple[CandidateTarget, BoundingBox, str]:
        candidate = CandidateTarget(
            target_id=target_id,
            role="slider",
            name="进度",
            text="进度",
            confidence=0.95,
            reasons=("测试",),
            recipe=LocatorRecipe("fake", role="slider", name="进度", backend_node_id=1),
            box=BoundingBox(10, 20, 100, 20),
        )
        return candidate, candidate.box or BoundingBox(10, 20, 100, 20), "object"


class CoalescedInputAckSession(StubSession):
    """模拟 Chrome：孤立输入延迟确认，收到完整序列后一次确认。"""

    def __init__(self, expected_input_events: int) -> None:
        super().__init__()
        self.expected_input_events = expected_input_events
        self.input_events = 0
        self.release_all = asyncio.Event()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = params or {}
        self.calls.append((method, payload))
        if method != "Input.dispatchMouseEvent":
            return {}
        self.input_events += 1
        if self.input_events >= self.expected_input_events:
            self.release_all.set()
        await self.release_all.wait()
        return {}


def test_pointer_click_pipelines_delayed_input_acknowledgements() -> None:
    async def scenario() -> None:
        session = CoalescedInputAckSession(expected_input_events=3)

        await asyncio.wait_for(dispatch_click(session, 20, 30), timeout=0.2)

        events = [
            payload for method, payload in session.calls if method == "Input.dispatchMouseEvent"
        ]
        assert [event["type"] for event in events] == [
            "mouseMoved",
            "mousePressed",
            "mouseReleased",
        ]

    asyncio.run(scenario())


def test_visual_drag_pipelines_delayed_input_acknowledgements() -> None:
    async def scenario() -> None:
        # 6 个接近点 + 按下 + 1 个拖动点 + 释放。
        session = CoalescedInputAckSession(expected_input_events=9)

        await asyncio.wait_for(
            dispatch_drag(session, ((100, 100, 0), (200, 100, 10)), approach=True),
            timeout=0.7,
        )

        events = [
            payload for method, payload in session.calls if method == "Input.dispatchMouseEvent"
        ]
        assert len(events) == 9
        assert events[-1]["type"] == "mouseReleased"

    asyncio.run(scenario())


def _command() -> ActionCommand:
    return ActionCommand(
        "drag",
        ActionKind.DRAG,
        target_id="slider",
        trajectory=(DragPoint(0, 0, 0), DragPoint(40, 2, 0), DragPoint(80, 0, 0)),
        drag_risk=DragRiskClass.BUSINESS,
    )


def test_driver_dispatches_complete_drag_sequence(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = DragDriver(BrowserConfig(), tmp_path)
        session = StubSession()
        driver.session = session  # type: ignore[assignment]

        receipt = await driver.execute(_command())

        assert receipt.success is True
        assert receipt.data["拖拽风险"] == "business"
        assert receipt.data["执行方式"] == "pointer"
        events = [
            payload for method, payload in session.calls if method == "Input.dispatchMouseEvent"
        ]
        assert [event["type"] for event in events] == [
            "mouseMoved",
            "mousePressed",
            "mouseMoved",
            "mouseMoved",
            "mouseReleased",
        ]
        assert events[0]["x"] == 60
        assert events[0]["y"] == 30
        assert events[-1]["x"] == 140
        assert events[-1]["y"] == 30
        assert events[1]["buttons"] == 1
        assert events[-1]["buttons"] == 0

    asyncio.run(scenario())


def test_driver_sets_native_range_value_and_reads_it_back(tmp_path: Path) -> None:
    class NativeRangeSession(StubSession):
        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            payload = params or {}
            self.calls.append((method, payload))
            if method == "Runtime.callFunctionOn":
                assert payload["arguments"] == [{"value": 1.0}]
                return {
                    "result": {
                        "value": {
                            "ok": True,
                            "previousValue": "50",
                            "actualValue": "100",
                            "minimum": 0,
                            "maximum": 100,
                            "step": 1,
                            "targetRatio": 1.0,
                        }
                    }
                }
            raise AssertionError(f"unexpected method: {method}")

    class NativeRangeDriver(CdpAutomationDriver):
        async def _resolve_target(
            self,
            target_id: str,
        ) -> tuple[CandidateTarget, BoundingBox, str]:
            box = BoundingBox(10, 20, 100, 20)
            locator = json.dumps(
                {"tag": "input", "attrs": {"type": "range"}},
                ensure_ascii=False,
            )
            candidate = CandidateTarget(
                target_id=target_id,
                role="slider",
                name="进度",
                text="进度",
                confidence=0.95,
                reasons=("测试",),
                recipe=LocatorRecipe(
                    "dom_backend_node",
                    value=locator,
                    role="slider",
                    name="进度",
                    backend_node_id=1,
                ),
                box=box,
                drag_risk=DragRiskClass.BUSINESS,
            )
            return candidate, box, "range-object"

    async def scenario() -> None:
        driver = NativeRangeDriver(BrowserConfig(), tmp_path)
        session = NativeRangeSession()
        driver.session = session  # type: ignore[assignment]

        receipt = await driver.execute(_command())

        assert receipt.success is True
        assert receipt.data["执行方式"] == "native_range"
        assert receipt.data["原值"] == "50"
        assert receipt.data["回读值"] == "100"
        assert receipt.data["目标比例"] == 1.0
        assert not any(method == "Input.dispatchMouseEvent" for method, _ in session.calls)

    asyncio.run(scenario())


def test_driver_releases_pointer_and_marks_interrupted_drag_unknown(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = DragDriver(BrowserConfig(), tmp_path)
        session = StubSession(fail_call=3)
        driver.session = session  # type: ignore[assignment]

        receipt = await driver.execute(_command())

        assert receipt.success is False
        assert receipt.outcome_known is False
        events = [
            payload for method, payload in session.calls if method == "Input.dispatchMouseEvent"
        ]
        assert [event["type"] for event in events] == [
            "mouseMoved",
            "mousePressed",
            "mouseMoved",
            "mouseReleased",
        ]
        assert events[-1]["buttons"] == 0

    asyncio.run(scenario())


def test_driver_releases_pointer_when_total_drag_budget_expires(tmp_path: Path) -> None:
    class StalledAfterPressSession(StubSession):
        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            payload = params or {}
            self.calls.append((method, payload))
            if (
                method == "Input.dispatchMouseEvent"
                and payload.get("type") == "mouseMoved"
                and payload.get("buttons") == 1
            ):
                await asyncio.Event().wait()
            return {}

    async def scenario() -> None:
        driver = DragDriver(BrowserConfig(), tmp_path)
        session = StalledAfterPressSession()
        driver.session = session  # type: ignore[assignment]
        command = ActionCommand(
            "drag-timeout",
            ActionKind.DRAG,
            target_id="slider",
            trajectory=(DragPoint(0, 0, 0), DragPoint(40, 2, 0)),
            drag_risk=DragRiskClass.BUSINESS,
            timeout_seconds=0.01,
        )

        receipt = await driver.execute(command)

        assert receipt.success is False
        assert receipt.outcome_known is False
        events = [
            payload for method, payload in session.calls if method == "Input.dispatchMouseEvent"
        ]
        assert events[-1]["type"] == "mouseReleased"
        assert receipt.duration_ms < 200

    asyncio.run(scenario())


def test_driver_converts_visual_ratios_using_current_css_viewport(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = DragDriver(BrowserConfig(), tmp_path)
        session = StubSession()
        driver.session = session  # type: ignore[assignment]
        driver._last_observation_fingerprint = "fingerprint"
        command = ActionCommand(
            "visual-drag",
            ActionKind.VISUAL_DRAG,
            visual_trajectory=(
                VisualDragPoint(0.1, 0.2, 0),
                VisualDragPoint(0.7, 0.8, 0),
            ),
            observation_fingerprint="fingerprint",
            screenshot_fingerprint=hashlib.sha256(b"stable-frame").hexdigest(),
            visual_confidence=0.9,
            drag_risk=DragRiskClass.BUSINESS,
        )

        receipt = await driver.execute(command)

        assert receipt.success is True
        assert receipt.data["拖拽风险"] == "business"
        assert receipt.data["执行方式"] == "pointer"
        assert receipt.data["可视指针反馈"] is True
        assert receipt.data["拖后像素变化"] is False
        assert receipt.data["起点命中"] == {"诊断可用": False, "命中": False}
        events = [
            payload for method, payload in session.calls if method == "Input.dispatchMouseEvent"
        ]
        press_index = next(
            index for index, event in enumerate(events) if event["type"] == "mousePressed"
        )
        assert press_index >= 5
        assert all(event["type"] == "mouseMoved" for event in events[:press_index])
        assert all(event["buttons"] == 0 for event in events[:press_index])
        assert events[press_index]["x"] == 100
        assert events[press_index]["y"] == 100
        assert events[press_index - 1]["x"] == 100
        assert events[press_index - 1]["y"] == 100
        assert events[-1]["x"] == 700
        assert events[-1]["y"] == 400
        overlay_methods = [method for method, _ in session.calls if method.startswith("Overlay.")]
        assert overlay_methods[0] == "Overlay.enable"
        assert overlay_methods.count("Overlay.highlightQuad") == len(events) - 2
        assert overlay_methods[-2:] == ["Overlay.hideHighlight", "Overlay.disable"]

    asyncio.run(scenario())


def test_visual_drag_continues_when_pointer_feedback_is_unavailable(tmp_path: Path) -> None:
    class NoOverlaySession(StubSession):
        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            if method == "Overlay.enable":
                self.calls.append((method, params or {}))
                raise CdpCommandError("Overlay 不可用", method=method, error_code=-32601)
            return await super().call(method, params, **kwargs)

    async def scenario() -> None:
        driver = DragDriver(BrowserConfig(), tmp_path)
        session = NoOverlaySession()
        driver.session = session  # type: ignore[assignment]
        driver._last_observation_fingerprint = "fingerprint"
        command = ActionCommand(
            "visual-drag-without-feedback",
            ActionKind.VISUAL_DRAG,
            visual_trajectory=(
                VisualDragPoint(0.1, 0.2, 0),
                VisualDragPoint(0.7, 0.8, 0),
            ),
            observation_fingerprint="fingerprint",
            screenshot_fingerprint=hashlib.sha256(b"stable-frame").hexdigest(),
            visual_confidence=0.9,
            drag_risk=DragRiskClass.BUSINESS,
        )

        receipt = await driver.execute(command)

        assert receipt.success is True
        assert receipt.data["可视指针反馈"] is False
        assert any(method == "Input.dispatchMouseEvent" for method, _ in session.calls)
        assert not any(method == "Overlay.highlightQuad" for method, _ in session.calls)

    asyncio.run(scenario())


def test_driver_stops_visual_drag_when_preflight_confirms_empty_start(tmp_path: Path) -> None:
    class EmptyHitSession(StubSession):
        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            if (
                method == "Runtime.evaluate"
                and params
                and "elementFromPoint" in str(params.get("expression", ""))
            ):
                return {"result": {"value": {"hit": False}}}
            return await super().call(method, params, **kwargs)

    async def scenario() -> None:
        driver = DragDriver(BrowserConfig(), tmp_path)
        session = EmptyHitSession()
        driver.session = session  # type: ignore[assignment]
        driver._last_observation_fingerprint = "fingerprint"
        command = ActionCommand(
            "visual-empty-start",
            ActionKind.VISUAL_DRAG,
            visual_trajectory=(
                VisualDragPoint(0.1, 0.2, 0),
                VisualDragPoint(0.7, 0.8, 0),
            ),
            observation_fingerprint="fingerprint",
            screenshot_fingerprint=hashlib.sha256(b"stable-frame").hexdigest(),
            visual_confidence=0.9,
            drag_risk=DragRiskClass.BUSINESS,
        )

        receipt = await driver.execute(command)

        assert receipt.success is False
        assert "起点未命中" in receipt.message
        assert not any(method == "Input.dispatchMouseEvent" for method, _ in session.calls)

    asyncio.run(scenario())


def test_driver_holds_pointer_before_first_visual_drag_move(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = DragDriver(BrowserConfig(), tmp_path)
        session = StubSession()
        driver.session = session  # type: ignore[assignment]
        driver._last_observation_fingerprint = "fingerprint"
        command = ActionCommand(
            "visual-drag-hold",
            ActionKind.VISUAL_DRAG,
            visual_trajectory=(
                VisualDragPoint(0.1, 0.2, 50),
                VisualDragPoint(0.7, 0.8, 0),
            ),
            observation_fingerprint="fingerprint",
            screenshot_fingerprint=hashlib.sha256(b"stable-frame").hexdigest(),
            visual_confidence=0.9,
            drag_risk=DragRiskClass.BUSINESS,
        )

        started = asyncio.get_running_loop().time()
        receipt = await driver.execute(command)
        elapsed = asyncio.get_running_loop().time() - started

        assert receipt.success is True
        assert elapsed >= 0.045

    asyncio.run(scenario())


def test_driver_clicks_current_visual_position(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = DragDriver(BrowserConfig(), tmp_path)
        session = StubSession()
        driver.session = session  # type: ignore[assignment]
        driver._last_observation_fingerprint = "fingerprint"
        command = ActionCommand(
            "visual-click",
            ActionKind.VISUAL_CLICK,
            visual_x_ratio=0.35,
            visual_y_ratio=0.04,
            observation_fingerprint="fingerprint",
            screenshot_fingerprint=hashlib.sha256(b"stable-frame").hexdigest(),
            visual_confidence=0.96,
        )

        receipt = await driver.execute(command)

        assert receipt.success is True
        events = [
            payload for method, payload in session.calls if method == "Input.dispatchMouseEvent"
        ]
        assert [event["type"] for event in events] == [
            "mouseMoved",
            "mousePressed",
            "mouseReleased",
        ]
        assert events[0]["x"] == 350
        assert events[0]["y"] == 20
        assert receipt.data["视觉置信度"] == 0.96

    asyncio.run(scenario())


def test_driver_captures_zoomed_visual_region(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = DragDriver(BrowserConfig(), tmp_path)
        session = StubSession()
        driver.session = session  # type: ignore[assignment]
        driver._last_observation_fingerprint = "fingerprint"
        command = ActionCommand(
            "inspect-region",
            ActionKind.INSPECT_VISUAL_REGION,
            visual_clip=(0.35, 0.35, 0.3, 0.2),
            observation_fingerprint="fingerprint",
            screenshot_fingerprint=hashlib.sha256(b"stable-frame").hexdigest(),
            visual_confidence=0.95,
            idempotent=True,
        )

        receipt = await driver.execute(command)

        assert receipt.success is True
        screenshot_calls = [
            payload for method, payload in session.calls if method == "Page.captureScreenshot"
        ]
        assert len(screenshot_calls) == 2
        assert screenshot_calls[1]["clip"] == {
            "x": 350.0,
            "y": 175.0,
            "width": 300.0,
            "height": 100.0,
            "scale": 2,
        }
        assert Path(receipt.data["path"]).exists()

    asyncio.run(scenario())


def test_driver_captures_region_when_pixels_change_but_observation_is_stable(
    tmp_path: Path,
) -> None:
    class DynamicFrameDriver(DragDriver):
        async def observe(self, *, force: bool = False) -> Observation:
            self._last_observation_fingerprint = "fingerprint"
            return Observation(
                "surface",
                "https://example.com/challenge",
                "滑动验证页面",
                1,
                "fingerprint",
                "请完成验证",
                (),
                visual_drag_risk=DragRiskClass.SECURITY,
            )

    async def scenario() -> None:
        driver = DynamicFrameDriver(BrowserConfig(), tmp_path)
        session = StubSession()
        driver.session = session  # type: ignore[assignment]
        driver._last_observation_fingerprint = "fingerprint"
        command = ActionCommand(
            "inspect-dynamic-region",
            ActionKind.INSPECT_VISUAL_REGION,
            visual_clip=(0.3, 0.3, 0.4, 0.3),
            observation_fingerprint="fingerprint",
            screenshot_fingerprint=hashlib.sha256(b"old-frame").hexdigest(),
            visual_confidence=0.95,
            idempotent=True,
        )

        receipt = await driver.execute(command)

        assert receipt.success is True
        assert Path(receipt.data["path"]).exists()

    asyncio.run(scenario())


def test_driver_rejects_visual_drag_when_screenshot_changed(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = DragDriver(BrowserConfig(), tmp_path)
        session = StubSession()
        driver.session = session  # type: ignore[assignment]
        driver._last_observation_fingerprint = "fingerprint"
        command = ActionCommand(
            "stale-visual-drag",
            ActionKind.VISUAL_DRAG,
            visual_trajectory=(
                VisualDragPoint(0.1, 0.2, 0),
                VisualDragPoint(0.7, 0.8, 0),
            ),
            observation_fingerprint="fingerprint",
            screenshot_fingerprint=hashlib.sha256(b"old-frame").hexdigest(),
            visual_confidence=0.9,
            drag_risk=DragRiskClass.BUSINESS,
        )

        receipt = await driver.execute(command)

        assert receipt.success is False
        assert receipt.outcome_known is True
        assert "截图已经变化" in receipt.message
        assert not any(method == "Input.dispatchMouseEvent" for method, _ in session.calls)

    asyncio.run(scenario())


def test_driver_rejects_visual_drag_when_observation_changed(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = DragDriver(BrowserConfig(), tmp_path)
        session = StubSession()
        driver.session = session  # type: ignore[assignment]
        driver._last_observation_fingerprint = "new-fingerprint"
        command = ActionCommand(
            "stale-observation-visual-drag",
            ActionKind.VISUAL_DRAG,
            visual_trajectory=(
                VisualDragPoint(0.1, 0.2, 0),
                VisualDragPoint(0.7, 0.8, 0),
            ),
            observation_fingerprint="old-fingerprint",
            screenshot_fingerprint=hashlib.sha256(b"stable-frame").hexdigest(),
            visual_confidence=0.9,
            drag_risk=DragRiskClass.BUSINESS,
        )

        receipt = await driver.execute(command)

        assert receipt.success is False
        assert "页面观察已经失效" in receipt.message
        assert not any(method == "Input.dispatchMouseEvent" for method, _ in session.calls)

    asyncio.run(scenario())


def test_authorized_security_drag_accepts_dynamic_frame_when_observation_is_stable(
    tmp_path: Path,
) -> None:
    class DynamicFrameDriver(DragDriver):
        async def observe(self, *, force: bool = False) -> Observation:
            self._last_observation_fingerprint = "fingerprint"
            return Observation(
                "surface",
                "https://example.com/challenge",
                "滑动验证页面",
                1,
                "fingerprint",
                "请完成验证",
                (),
                visual_drag_risk=DragRiskClass.SECURITY,
            )

    async def scenario() -> None:
        driver = DynamicFrameDriver(BrowserConfig(), tmp_path)
        session = StubSession()
        driver.session = session  # type: ignore[assignment]
        driver._last_observation_fingerprint = "fingerprint"
        command = ActionCommand(
            "dynamic-security-drag",
            ActionKind.VISUAL_DRAG,
            visual_trajectory=(
                VisualDragPoint(0.1, 0.2, 0),
                VisualDragPoint(0.7, 0.2, 0),
            ),
            observation_fingerprint="fingerprint",
            screenshot_fingerprint=hashlib.sha256(b"old-frame").hexdigest(),
            visual_confidence=0.95,
            security_challenge=True,
            drag_risk=DragRiskClass.SECURITY,
            allow_dynamic_visual_frame=True,
        )

        receipt = await driver.execute(command)

        assert receipt.success is True
        assert receipt.data["动态视觉帧"] is True
        assert any(method == "Input.dispatchMouseEvent" for method, _ in session.calls)

    asyncio.run(scenario())


def test_dynamic_security_drag_rejects_changed_observation(tmp_path: Path) -> None:
    class ChangedFrameDriver(DragDriver):
        async def observe(self, *, force: bool = False) -> Observation:
            self._last_observation_fingerprint = "changed"
            return Observation(
                "surface",
                "https://example.com/other",
                "其他页面",
                2,
                "changed",
                "页面已经变化",
                (),
            )

    async def scenario() -> None:
        driver = ChangedFrameDriver(BrowserConfig(), tmp_path)
        session = StubSession()
        driver.session = session  # type: ignore[assignment]
        driver._last_observation_fingerprint = "fingerprint"
        command = ActionCommand(
            "changed-dynamic-security-drag",
            ActionKind.VISUAL_DRAG,
            visual_trajectory=(
                VisualDragPoint(0.1, 0.2, 0),
                VisualDragPoint(0.7, 0.2, 0),
            ),
            observation_fingerprint="fingerprint",
            screenshot_fingerprint=hashlib.sha256(b"old-frame").hexdigest(),
            visual_confidence=0.95,
            security_challenge=True,
            drag_risk=DragRiskClass.SECURITY,
            allow_dynamic_visual_frame=True,
        )

        receipt = await driver.execute(command)

        assert receipt.success is False
        assert "页面观察已经变化" in receipt.message
        assert not any(method == "Input.dispatchMouseEvent" for method, _ in session.calls)

    asyncio.run(scenario())


def test_driver_creates_private_evidence_file(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = DragDriver(BrowserConfig(), tmp_path)
        driver.session = StubSession()  # type: ignore[assignment]

        path = await driver.capture_evidence("private")

        assert path.read_bytes() == b"stable-frame"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    asyncio.run(scenario())

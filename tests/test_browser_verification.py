from __future__ import annotations

import asyncio
from typing import ClassVar

from witty_browser_auto.browser.verification import verify_condition
from witty_browser_auto.domain.models import (
    BoundingBox,
    CandidateTarget,
    DragRiskClass,
    ExpectedCondition,
    LocatorRecipe,
    Observation,
)


class StubSession:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls = 0

    async def call(self, method: str, params=None):
        text = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        return {"result": {"value": {"text": text, "ready": "complete"}}}


class StubDriver:
    _candidate_cache: ClassVar[dict] = {}

    def __init__(
        self,
        texts: list[str],
        fingerprint: str = "after",
        observation: Observation | None = None,
    ) -> None:
        self.session = StubSession(texts)
        self.fingerprint = fingerprint
        self.observation = observation
        self.observations = 0

    def _require_session(self):
        return self.session

    async def observe(self, *, force: bool = False) -> Observation:
        self.observations += 1
        if self.observation is not None:
            return self.observation
        return Observation("surface", "https://example.com", "", 1, self.fingerprint, "", ())

    def _matches_locator_recipe(self, previous, current) -> bool:
        return False


def test_challenge_verification_stops_immediately_on_explicit_failure() -> None:
    async def scenario() -> None:
        driver = StubDriver(["验证失败，请刷新"])

        result = await verify_condition(
            driver,
            ExpectedCondition("challenge_cleared", "验证您是真人", 5),
        )

        assert result.success is False
        assert result.reason == "安全挑战已明确返回失败"
        assert driver.session.calls == 1

    asyncio.run(scenario())


def test_fingerprint_verification_waits_for_delayed_spa_change() -> None:
    class DelayedDriver(StubDriver):
        async def observe(self, *, force: bool = False) -> Observation:
            self.observations += 1
            fingerprint = "before" if self.observations < 4 else "after"
            return Observation("surface", "https://example.com", "", 1, fingerprint, "", ())

    async def scenario() -> None:
        driver = DelayedDriver([])

        result = await verify_condition(
            driver,
            ExpectedCondition("fingerprint_changed", "before", 0.3),
        )

        assert result.success is True
        assert result.reason == "页面状态已变化"
        assert driver.observations == 4

    asyncio.run(scenario())


def test_fingerprint_verification_uses_full_window_before_failure() -> None:
    async def scenario() -> None:
        driver = StubDriver([], fingerprint="before")
        started = asyncio.get_running_loop().time()

        result = await verify_condition(
            driver,
            ExpectedCondition("fingerprint_changed", "before", 0.12),
        )

        assert result.success is False
        assert driver.observations >= 3
        assert asyncio.get_running_loop().time() - started >= 0.1

    asyncio.run(scenario())


def test_challenge_verification_requires_two_stable_clear_checks() -> None:
    async def scenario() -> None:
        driver = StubDriver(["订单页面", "订单页面"])

        result = await verify_condition(
            driver,
            ExpectedCondition("challenge_cleared", "验证您是真人", 1),
        )

        assert result.success is True
        assert result.reason == "安全挑战页面已消失"
        assert driver.session.calls == 2

    asyncio.run(scenario())


def test_page_visible_verification_waits_without_observing_dom() -> None:
    class VisibilitySession:
        def __init__(self) -> None:
            self.states = ["hidden", "visible"]
            self.calls = 0

        async def call(self, method: str, params=None):
            assert method == "Runtime.evaluate"
            state = self.states[min(self.calls, len(self.states) - 1)]
            self.calls += 1
            return {"result": {"value": state}}

    async def scenario() -> None:
        driver = StubDriver([])
        driver.session = VisibilitySession()

        result = await verify_condition(
            driver,
            ExpectedCondition("page_visible", "visible", 0.2),
        )

        assert result.success is True
        assert result.reason == "浏览器页面已恢复可见"
        assert driver.session.calls == 2
        assert driver.observations == 0

    asyncio.run(scenario())


def test_challenge_ready_requires_rendered_drag_track() -> None:
    async def scenario() -> None:
        rendering = Observation(
            "surface",
            "https://example.com/challenge",
            "验证",
            1,
            "rendering",
            "控件渲染中",
            (),
            visual_drag_risk=DragRiskClass.SECURITY,
        )
        track = CandidateTarget(
            target_id="slider-track",
            role="slider",
            name="滑动验证",
            text="",
            confidence=0.98,
            reasons=("细长拖拽轨道",),
            recipe=LocatorRecipe("fake", role="slider", name="滑动验证"),
            box=BoundingBox(100, 200, 320, 40),
            drag_risk=DragRiskClass.SECURITY,
        )
        ready = Observation(
            "surface",
            "https://example.com/challenge",
            "验证",
            2,
            "ready",
            "控件已显示",
            (track,),
            visual_drag_risk=DragRiskClass.SECURITY,
        )
        failed = Observation(
            "surface",
            "https://example.com/challenge",
            "验证",
            3,
            "failed",
            "验证失败，请刷新",
            (track,),
            visual_drag_risk=DragRiskClass.SECURITY,
        )
        condition = ExpectedCondition("challenge_ready", "security_drag", 0.5)

        pending_result = await verify_condition(
            StubDriver(["验证"], observation=rendering),
            condition,
        )
        ready_result = await verify_condition(
            StubDriver(["验证"], observation=ready),
            condition,
        )
        failed_result = await verify_condition(
            StubDriver(["验证失败，请刷新"], observation=failed),
            ExpectedCondition("challenge_ready", "security_drag", 0.05),
        )

        assert pending_result.success is False
        assert pending_result.reason == "安全挑战控件仍在渲染"
        assert ready_result.success is True
        assert ready_result.reason == "安全挑战控件已就绪"
        assert failed_result.success is False

    asyncio.run(scenario())


def test_challenge_refresh_requires_new_ready_observation() -> None:
    class SequenceDriver(StubDriver):
        def __init__(self, observations: list[Observation]) -> None:
            super().__init__([])
            self.observation_sequence = observations

        async def observe(self, *, force: bool = False) -> Observation:
            index = min(self.observations, len(self.observation_sequence) - 1)
            self.observations += 1
            return self.observation_sequence[index]

    async def scenario() -> None:
        track = CandidateTarget(
            target_id="slider-track",
            role="slider",
            name="滑动验证",
            text="",
            confidence=0.98,
            reasons=("细长拖拽轨道",),
            recipe=LocatorRecipe("fake", role="slider", name="滑动验证"),
            box=BoundingBox(100, 200, 320, 40),
            drag_risk=DragRiskClass.SECURITY,
        )
        failed = Observation(
            "surface",
            "https://example.com/challenge",
            "滑动验证",
            1,
            "old-fingerprint",
            "验证失败，请刷新",
            (track,),
            visual_drag_risk=DragRiskClass.SECURITY,
        )
        ready = Observation(
            "surface",
            "https://example.com/challenge",
            "滑动验证",
            2,
            "new-fingerprint",
            "请按住滑块，拖动到最右边",
            (track,),
            visual_drag_risk=DragRiskClass.SECURITY,
        )

        result = await verify_condition(
            SequenceDriver([failed, ready]),
            ExpectedCondition("challenge_refreshed", "old-fingerprint", 0.2),
        )

        assert result.success is True
        assert result.reason == "安全挑战已刷新并重新就绪"

    asyncio.run(scenario())

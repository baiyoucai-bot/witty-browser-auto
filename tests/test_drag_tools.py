from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from witty_browser_auto.agent.tools import ToolExecutor
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionKind,
    ActionReceipt,
    BoundingBox,
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
from witty_browser_auto.runtime.repair import ToolFailureKind


class RecordingDriver:
    capabilities = DriverCapabilities(dom=True, accessibility=True)

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root
        self.commands: list[ActionCommand] = []
        self.evidence_count = 0
        self.fail_evidence = False

    async def start(self) -> None:
        return None

    async def open(self, url: str) -> str:
        return "surface"

    async def observe(self, *, force: bool = False) -> Observation:
        return _observation()

    async def execute(self, command: ActionCommand) -> ActionReceipt:
        self.commands.append(command)
        return ActionReceipt(command.action_id, True, True, "已执行", 1.0)

    async def verify(self, condition: object) -> VerificationResult:
        return VerificationResult(True, "校验通过")

    async def capture_evidence(self, label: str) -> Path:
        if self.fail_evidence:
            raise RuntimeError("模拟截图失败")
        self.evidence_count += 1
        path = self.artifact_root / f"{label}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return path

    async def close(self) -> None:
        return None


def _observation(
    *,
    title: str = "滑块测试",
    summary: str = "普通业务滑块",
    candidate_risk: DragRiskClass = DragRiskClass.BUSINESS,
    visual_risk: DragRiskClass = DragRiskClass.BUSINESS,
) -> Observation:
    return Observation(
        surface_id="surface",
        url="https://example.com/slider",
        title=title,
        version=1,
        fingerprint="fingerprint",
        summary=summary,
        candidates=(
            CandidateTarget(
                target_id="slider-1",
                role="slider",
                name="进度",
                text="进度",
                confidence=0.95,
                reasons=("测试",),
                recipe=LocatorRecipe("fake", role="slider", name="进度"),
                box=BoundingBox(10, 20, 200, 30),
                drag_risk=candidate_risk,
                drag_risk_reasons=("测试风险分类",),
            ),
        ),
        visual_drag_risk=visual_risk,
        visual_drag_risk_reasons=("测试风险分类",),
    )


def _drag_call(*, security_challenge: bool = False) -> ModelToolCall:
    return ModelToolCall(
        "drag-call",
        "drag",
        {
            "target_id": "slider-1",
            "end_dx": 160,
            "end_dy": 0,
            "duration_ms": 400,
            "steps": 9,
            "security_challenge": security_challenge,
            "expect_kind": "text_contains",
            "expect_value": "已完成",
        },
    )


def _visual_drag_call(
    *,
    fingerprint: str = "fingerprint",
    security_challenge: bool = False,
    motion_profile: str = "balanced",
    geometry_mode: str = "track",
) -> ModelToolCall:
    return ModelToolCall(
        "visual-drag-call",
        "visual_drag",
        {
            "observation_fingerprint": fingerprint,
            "screenshot_fingerprint": "screenshot",
            "start_x_ratio": 0.1,
            "start_y_ratio": 0.5,
            "end_x_ratio": 0.8,
            "end_y_ratio": 0.5,
            "duration_ms": 400,
            "steps": 9,
            "motion_profile": motion_profile,
            "geometry_mode": geometry_mode,
            "visual_confidence": 0.9,
            "security_challenge": security_challenge,
            "expect_kind": "text_contains",
            "expect_value": "已完成",
        },
    )


def test_input_text_uses_code_readback_without_model_postcondition(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        task = TaskSpec(
            "input-by-key",
            "输入查询账号",
            "https://example.com/slider",
            ExecutionScope("project"),
            inputs={"account": "sensitive-value"},
        )
        observation = replace(
            _observation(),
            candidates=(
                CandidateTarget(
                    target_id="account-input",
                    role="textbox",
                    name="查询账号",
                    text="",
                    confidence=0.95,
                    reasons=("测试",),
                    recipe=LocatorRecipe("fake", role="textbox", name="查询账号"),
                    box=BoundingBox(10, 20, 200, 30),
                ),
            ),
        )
        result = await ToolExecutor(driver, task).execute(
            ModelToolCall(
                "input-call",
                "input_text",
                {"target_id": "account-input", "input_key": "account"},
            ),
            observation,
        )

        assert result.success is True
        assert driver.commands[0].kind is ActionKind.INPUT_TEXT
        assert driver.commands[0].expected is None
        assert driver.commands[0].value == "sensitive-value"
        assert result.plan_step is not None
        assert result.plan_step.input_key == "account"

    asyncio.run(scenario())


def test_clicking_textbox_to_focus_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        task = TaskSpec(
            "focus-input",
            "填写查询账号",
            "https://example.com/slider",
            ExecutionScope("project"),
        )
        observation = replace(
            _observation(),
            candidates=(
                CandidateTarget(
                    target_id="account-input",
                    role="textbox",
                    name="查询账号",
                    text="",
                    confidence=0.95,
                    reasons=("测试",),
                    recipe=LocatorRecipe("fake", role="textbox", name="查询账号"),
                    box=BoundingBox(10, 20, 200, 30),
                ),
            ),
        )
        result = await ToolExecutor(driver, task).execute(
            ModelToolCall(
                "focus-call",
                "click",
                {
                    "target_id": "account-input",
                    "expect_kind": "fingerprint_changed",
                    "expect_value": observation.fingerprint,
                },
            ),
            observation,
        )

        assert result.success is True
        assert driver.commands[0].kind is ActionKind.CLICK
        assert driver.commands[0].idempotent is True

    asyncio.run(scenario())


def test_read_only_click_replaces_preexisting_postcondition(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        task = TaskSpec(
            "query-click",
            "查询订单",
            "https://example.com/order",
            ExecutionScope("project"),
        )
        observation = replace(
            _observation(summary="查询订单"),
            candidates=(
                CandidateTarget(
                    target_id="query-button",
                    role="button",
                    name="查询订单",
                    text="查询订单",
                    confidence=0.95,
                    reasons=("测试",),
                    recipe=LocatorRecipe("fake", role="button", name="查询订单"),
                    box=BoundingBox(10, 20, 100, 30),
                ),
            ),
        )
        result = await ToolExecutor(driver, task).execute(
            ModelToolCall(
                "query-call",
                "click",
                {
                    "target_id": "query-button",
                    "expect_kind": "text_contains",
                    "expect_value": "查询订单",
                },
            ),
            observation,
        )

        assert result.success is True
        assert driver.commands[0].expected is not None
        assert driver.commands[0].expected.kind == "fingerprint_changed"
        assert driver.commands[0].expected.value == observation.fingerprint
        assert driver.commands[0].idempotent is True

    asyncio.run(scenario())


def test_challenge_refresh_reloads_when_click_does_not_restore_slider(tmp_path: Path) -> None:
    class RefreshDriver(RecordingDriver):
        def __init__(self, artifact_root: Path) -> None:
            super().__init__(artifact_root)
            self.verifications = 0

        async def verify(self, condition: object) -> VerificationResult:
            self.verifications += 1
            return VerificationResult(
                self.verifications >= 2,
                "安全挑战已刷新" if self.verifications >= 2 else "仍为失败态",
            )

    async def scenario() -> None:
        driver = RefreshDriver(tmp_path)
        task = TaskSpec(
            "refresh-challenge",
            "刷新验证码",
            "https://example.com/challenge",
            ExecutionScope("project"),
            allow_security_challenge=True,
        )
        refresh = CandidateTarget(
            "captcha-refresh",
            "button",
            "captcha-sliding-refresh",
            "刷新",
            0.95,
            ("验证码刷新",),
            LocatorRecipe("fake", role="button", name="captcha-sliding-refresh"),
            box=BoundingBox(300, 200, 20, 20),
        )
        observation = replace(
            _observation(
                title="滑动验证页面",
                summary="验证失败，请刷新",
                visual_risk=DragRiskClass.SECURITY,
            ),
            url="https://example.com/challenge",
            candidates=(refresh,),
        )

        result = await ToolExecutor(driver, task).execute(
            ModelToolCall("refresh", "click", {"target_id": refresh.target_id}),
            observation,
        )

        assert result.success is True
        assert result.counts_as_action is False
        assert result.data["challenge_reload_fallback"] is True
        assert [command.kind for command in driver.commands] == [
            ActionKind.CLICK,
            ActionKind.NAVIGATE,
        ]
        assert driver.commands[0].expected is not None
        assert driver.commands[0].expected.kind == "challenge_refreshed"
        assert driver.commands[1].url == observation.url

    asyncio.run(scenario())


def test_click_can_require_current_page_fingerprint_to_change(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        task = TaskSpec(
            "confirm-query",
            "确认查询订单",
            "https://example.com/order",
            ExecutionScope("project"),
        )
        observation = replace(
            _observation(summary="确认查询"),
            candidates=(
                CandidateTarget(
                    target_id="confirm-button",
                    role="button",
                    name="确定",
                    text="确定",
                    confidence=0.95,
                    reasons=("测试",),
                    recipe=LocatorRecipe("fake", role="button", name="确定"),
                    box=BoundingBox(10, 20, 100, 30),
                ),
            ),
        )
        result = await ToolExecutor(driver, task).execute(
            ModelToolCall(
                "confirm-call",
                "click",
                {
                    "target_id": "confirm-button",
                    "expect_kind": "fingerprint_changed",
                    "expect_value": observation.fingerprint,
                },
            ),
            observation,
        )

        assert result.success is True
        assert driver.commands[0].expected == ExpectedCondition(
            "fingerprint_changed", observation.fingerprint, timeout_seconds=4.0
        )
        assert driver.commands[0].idempotent is False

    asyncio.run(scenario())


def test_click_rejects_stale_page_fingerprint(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        task = TaskSpec(
            "stale-confirm",
            "确认查询订单",
            "https://example.com/order",
            ExecutionScope("project"),
        )
        observation = replace(
            _observation(summary="确认查询"),
            candidates=(
                CandidateTarget(
                    target_id="confirm-button",
                    role="button",
                    name="确定",
                    text="确定",
                    confidence=0.95,
                    reasons=("测试",),
                    recipe=LocatorRecipe("fake", role="button", name="确定"),
                    box=BoundingBox(10, 20, 100, 30),
                ),
            ),
        )
        result = await ToolExecutor(driver, task).execute(
            ModelToolCall(
                "stale-confirm-call",
                "click",
                {
                    "target_id": "confirm-button",
                    "expect_kind": "fingerprint_changed",
                    "expect_value": "stale-fingerprint",
                },
            ),
            observation,
        )

        assert result.success is False
        assert "绑定当前观察" in result.message
        assert driver.commands == []

    asyncio.run(scenario())


def test_challenge_submit_binds_stale_fingerprint_to_current_observation(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        task = TaskSpec(
            "challenge-submit-fingerprint",
            "识别验证码并提交",
            "https://example.com/order",
            ExecutionScope("project"),
            allow_security_challenge=True,
            allow_visual_actions=True,
        )
        base = _captcha_observation()
        observation = replace(
            base,
            candidates=(
                *base.candidates,
                CandidateTarget(
                    target_id="captcha-submit",
                    role="button",
                    name="提交验证码",
                    text="提交",
                    confidence=0.95,
                    reasons=("测试",),
                    recipe=LocatorRecipe("fake", role="button", name="提交验证码"),
                    box=BoundingBox(320, 100, 80, 40),
                ),
            ),
        )
        executor = ToolExecutor(
            driver,
            task,
            visual_context_available=True,
        )
        entered = await executor.execute(_generated_text_call(), observation)
        submitted = await executor.execute(
            ModelToolCall(
                "challenge-submit-call",
                "click",
                {
                    "target_id": "captcha-submit",
                    "expect_kind": "fingerprint_changed",
                    "expect_value": "stale-fingerprint",
                },
            ),
            observation,
        )

        assert entered.success is True
        assert submitted.success is True
        assert driver.commands[-1].expected == ExpectedCondition(
            "fingerprint_changed", observation.fingerprint, timeout_seconds=4.0
        )

    asyncio.run(scenario())


def _visual_click_call(*, fingerprint: str = "fingerprint") -> ModelToolCall:
    return ModelToolCall(
        "visual-click-call",
        "visual_click",
        {
            "observation_fingerprint": fingerprint,
            "screenshot_fingerprint": hashlib.sha256(b"png").hexdigest(),
            "x_ratio": 0.35,
            "y_ratio": 0.04,
            "visual_confidence": 0.96,
            "expect_kind": "url_contains",
            "expect_value": "/order",
        },
    )


def _inspect_visual_region_call() -> ModelToolCall:
    return ModelToolCall(
        "inspect-region-call",
        "inspect_visual_region",
        {
            "observation_fingerprint": "fingerprint",
            "screenshot_fingerprint": hashlib.sha256(b"png").hexdigest(),
            "x_ratio": 0.35,
            "y_ratio": 0.35,
            "width_ratio": 0.3,
            "height_ratio": 0.2,
            "visual_confidence": 0.95,
        },
    )


def _generated_text_call(
    *,
    screenshot_fingerprint: str | None = None,
    security_challenge: bool = True,
    text: str = "mDAF",
) -> ModelToolCall:
    return ModelToolCall(
        "generated-input-call",
        "input_generated_text",
        {
            "target_id": "captcha-input",
            "text": text,
            "observation_fingerprint": "fingerprint",
            "screenshot_fingerprint": screenshot_fingerprint or hashlib.sha256(b"png").hexdigest(),
            "visual_confidence": 0.95,
            "security_challenge": security_challenge,
        },
    )


def _captcha_observation(*, input_type: str = "text") -> Observation:
    candidate = CandidateTarget(
        target_id="captcha-input",
        role="textbox",
        name="请输入验证码",
        text="请输入验证码",
        confidence=0.95,
        reasons=("测试",),
        recipe=LocatorRecipe(
            "fake",
            value=json.dumps({"attrs": {"type": input_type}}),
            role="textbox",
            name="请输入验证码",
        ),
        box=BoundingBox(100, 100, 200, 40),
    )
    return Observation(
        surface_id="surface",
        url="https://example.com/order",
        title="订单查询",
        version=1,
        fingerprint="fingerprint",
        summary="请输入图片验证码",
        candidates=(candidate,),
        visual_drag_risk=DragRiskClass.SECURITY,
        visual_drag_risk_reasons=("页面包含明确的人机验证信号",),
    )


def test_generated_text_input_requires_multimodal_context_and_current_screenshot(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        task = TaskSpec(
            "generated-input",
            "识别并填写图片验证码",
            "https://example.com/order",
            ExecutionScope("project"),
            allow_security_challenge=True,
            allow_visual_actions=True,
        )
        no_image_driver = RecordingDriver(tmp_path / "no-image")
        no_image = await ToolExecutor(no_image_driver, task).execute(
            _generated_text_call(),
            _captcha_observation(),
        )
        stale_driver = RecordingDriver(tmp_path / "stale")
        stale = await ToolExecutor(
            stale_driver,
            task,
            visual_context_available=True,
        ).execute(
            _generated_text_call(screenshot_fingerprint="stale"),
            _captcha_observation(),
        )

        assert no_image.success is False
        assert "模型图片输入未启用" in no_image.message
        assert stale.success is False
        assert "截图已经变化" in stale.message
        assert not no_image_driver.commands
        assert not stale_driver.commands

    asyncio.run(scenario())


def test_generated_text_input_rejects_password_and_requires_challenge_authorization(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        base_task = TaskSpec(
            "generated-input-policy",
            "识别并填写图片验证码",
            "https://example.com/order",
            ExecutionScope("project"),
            allow_visual_actions=True,
        )
        password_driver = RecordingDriver(tmp_path / "password")
        password = await ToolExecutor(
            password_driver,
            replace(base_task, allow_security_challenge=True),
            visual_context_available=True,
        ).execute(_generated_text_call(), _captcha_observation(input_type="password"))
        denied_driver = RecordingDriver(tmp_path / "denied")
        denied = await ToolExecutor(
            denied_driver,
            base_task,
            visual_context_available=True,
        ).execute(_generated_text_call(), _captcha_observation())

        assert password.success is False
        assert "密码框" in password.message
        assert denied.success is False
        assert "未授权处理安全挑战" in denied.message
        assert not password_driver.commands
        assert not denied_driver.commands

    asyncio.run(scenario())


def test_generated_text_input_is_not_persisted_as_fast_path(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        task = TaskSpec(
            "generated-input-success",
            "识别并填写图片验证码",
            "https://example.com/order",
            ExecutionScope("project"),
            allow_security_challenge=True,
            allow_visual_actions=True,
        )
        result = await ToolExecutor(
            driver,
            task,
            visual_context_available=True,
        ).execute(_generated_text_call(), _captcha_observation())

        assert result.success is True
        assert result.plan_step is None
        assert result.evidence is not None
        assert result.evidence.kind == "model_generated_text_before"
        assert driver.commands[0].kind is ActionKind.INPUT_TEXT
        assert driver.commands[0].value == "mDAF"
        assert driver.commands[0].expected is None
        executor = ToolExecutor(
            RecordingDriver(tmp_path / "state"),
            task,
            visual_context_available=True,
        )
        state_result = await executor.execute(_generated_text_call(), _captcha_observation())
        assert state_result.success is True
        assert executor.security_challenge_active is True
        assert executor.security_challenge_text_entered is True
        assert executor.high_risk_drag_attempts == 1

    asyncio.run(scenario())


def test_generated_text_rejects_same_answer_for_same_captcha_without_spending_budget(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        executor = ToolExecutor(
            driver,
            TaskSpec(
                "generated-input-deduplicated",
                "识别并填写图片验证码",
                "https://example.com/order",
                ExecutionScope("project"),
                allow_security_challenge=True,
                allow_visual_actions=True,
                max_security_challenge_attempts=3,
            ),
            visual_context_available=True,
        )

        first = await executor.execute(_generated_text_call(), _captcha_observation())
        repeated = await executor.execute(_generated_text_call(), _captcha_observation())

        assert first.success is True
        assert repeated.success is False
        assert repeated.counts_as_action is False
        assert repeated.failure_kind is ToolFailureKind.POLICY
        assert repeated.data["reason"] == "repeated_challenge_strategy"
        assert executor.high_risk_drag_attempts == 1
        assert len(executor.security_challenge_text_signatures) == 1
        assert len(driver.commands) == 1

    asyncio.run(scenario())


def test_generated_text_input_trims_outer_whitespace(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        task = TaskSpec(
            "generated-input-trim",
            "识别并填写图片验证码",
            "https://example.com/order",
            ExecutionScope("project"),
            allow_security_challenge=True,
            allow_visual_actions=True,
        )
        result = await ToolExecutor(
            driver,
            task,
            visual_context_available=True,
        ).execute(
            _generated_text_call(text="  mD\nAF\t"),
            _captcha_observation(),
        )

        assert result.success is True
        assert driver.commands[0].value == "mDAF"

    asyncio.run(scenario())


def test_visual_click_requires_permission_and_current_observation(tmp_path: Path) -> None:
    async def scenario() -> None:
        denied_driver = RecordingDriver(tmp_path / "denied")
        denied = await ToolExecutor(
            denied_driver,
            TaskSpec(
                "visual-click-denied",
                "点击截图中的订单入口",
                "https://example.com/slider",
                ExecutionScope("project"),
            ),
            visual_context_available=True,
        ).execute(_visual_click_call(), _observation())
        allowed_driver = RecordingDriver(tmp_path / "allowed")
        executor = ToolExecutor(
            allowed_driver,
            TaskSpec(
                "visual-click-allowed",
                "点击截图中的订单入口",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_visual_actions=True,
            ),
            visual_context_available=True,
        )
        stale = await executor.execute(_visual_click_call(fingerprint="stale"), _observation())
        current = await executor.execute(_visual_click_call(), _observation())

        assert denied.success is False
        assert "未授权视觉" in denied.message
        assert stale.success is False
        assert "页面观察已经失效" in stale.message
        assert current.success is True
        assert current.plan_step is None
        assert allowed_driver.commands[0].kind is ActionKind.VISUAL_CLICK
        assert allowed_driver.commands[0].visual_x_ratio == 0.35
        assert allowed_driver.commands[0].visual_y_ratio == 0.04

    asyncio.run(scenario())


def test_visual_region_inspection_is_read_only_and_not_persisted(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        result = await ToolExecutor(
            driver,
            TaskSpec(
                "inspect-visual-region",
                "放大观察图片验证码",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_visual_actions=True,
            ),
            visual_context_available=True,
        ).execute(_inspect_visual_region_call(), _observation())

        assert result.success is True
        assert result.idempotent is True
        assert result.plan_step is None
        assert driver.commands[0].kind is ActionKind.INSPECT_VISUAL_REGION
        assert driver.commands[0].visual_clip == (0.35, 0.35, 0.3, 0.2)

    asyncio.run(scenario())


def test_drag_tool_builds_smooth_non_replayable_command(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        executor = ToolExecutor(
            driver,
            TaskSpec(
                "drag-task",
                "拖动普通业务滑块",
                "https://example.com/slider",
                ExecutionScope("project"),
            ),
        )

        result = await executor.execute(_drag_call(), _observation())

        assert result.success is True
        assert result.plan_step is None
        command = driver.commands[0]
        assert command.kind is ActionKind.DRAG
        assert command.idempotent is False
        assert len(command.trajectory) == 9
        assert command.trajectory[0].dx == 0
        assert command.trajectory[-1].dx == 160

    asyncio.run(scenario())


def test_security_challenge_requires_explicit_task_authorization(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        executor = ToolExecutor(
            driver,
            TaskSpec(
                "blocked-challenge",
                "处理滑块",
                "https://example.com/slider",
                ExecutionScope("project"),
            ),
        )

        result = await executor.execute(_drag_call(security_challenge=True), _observation())

        assert result.success is False
        assert "未授权处理安全挑战" in result.message
        assert not driver.commands
        assert driver.evidence_count == 0

    asyncio.run(scenario())


def test_suspected_security_challenge_cannot_be_downgraded_by_model(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        executor = ToolExecutor(
            driver,
            TaskSpec(
                "challenge-mislabeled",
                "处理滑动验证",
                "https://example.com/slider",
                ExecutionScope("project"),
            ),
        )
        challenge_observation = _observation(
            title="滑动验证页面",
            summary="请拖动滑块完成真人验证",
            candidate_risk=DragRiskClass.SECURITY,
            visual_risk=DragRiskClass.SECURITY,
        )

        result = await executor.execute(_drag_call(), challenge_observation)

        assert result.success is False
        assert "疑似安全挑战" in result.message
        assert not driver.commands
        assert driver.evidence_count == 0

    asyncio.run(scenario())


def test_security_evidence_failure_does_not_consume_attempt_budget(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        driver.fail_evidence = True
        executor = ToolExecutor(
            driver,
            TaskSpec(
                "challenge-evidence-failure",
                "处理已授权滑块",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_security_challenge=True,
                max_security_challenge_attempts=1,
            ),
        )

        failed = await executor.execute(_drag_call(security_challenge=True), _observation())
        driver.fail_evidence = False
        retried = await executor.execute(_drag_call(security_challenge=True), _observation())

        assert failed.success is False
        assert "无法保存截图证据" in failed.message
        assert retried.success is True
        assert len(driver.commands) == 1
        assert driver.evidence_count == 1

    asyncio.run(scenario())


def test_security_challenge_rejects_equivalent_strategy_without_dispatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        executor = ToolExecutor(
            driver,
            TaskSpec(
                "allowed-challenge",
                "处理已授权滑块",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_security_challenge=True,
                max_security_challenge_attempts=1,
            ),
        )

        first = await executor.execute(_drag_call(security_challenge=True), _observation())
        second = await executor.execute(_drag_call(security_challenge=True), _observation())

        assert first.success is True
        assert first.evidence is not None
        assert first.evidence.kind == "security_challenge_before"
        assert second.success is False
        assert "等价语义拖拽策略" in second.message
        assert second.data["reason"] == "repeated_challenge_strategy"
        assert second.counts_as_action is False
        assert len(driver.commands) == 1
        assert driver.evidence_count == 1

    asyncio.run(scenario())


def test_visual_security_drag_snaps_to_public_track_geometry(tmp_path: Path) -> None:
    async def scenario() -> None:
        observation = replace(
            _observation(
                title="滑动验证页面",
                summary="请完成以下操作，验证您是真人",
                candidate_risk=DragRiskClass.SECURITY,
                visual_risk=DragRiskClass.SECURITY,
            ),
            candidates=(
                replace(
                    _observation().candidates[0],
                    box=BoundingBox(460, 402, 280, 20),
                    drag_risk=DragRiskClass.SECURITY,
                ),
            ),
            metadata={"CSS视口": {"width": 1200, "height": 924}},
        )
        driver = RecordingDriver(tmp_path)
        result = await ToolExecutor(
            driver,
            TaskSpec(
                "geometry-snap",
                "处理滑动验证",
                observation.url,
                ExecutionScope("project"),
                allow_security_challenge=True,
                allow_visual_actions=True,
                max_security_challenge_attempts=3,
            ),
            visual_context_available=True,
        ).execute(
            _visual_drag_call(security_challenge=True),
            observation,
        )

        assert result.success is True
        command = driver.commands[0]
        assert command.expected is not None
        assert command.expected.kind == "challenge_cleared"
        assert command.visual_trajectory[0].x_ratio == 440 / 1200
        assert command.visual_trajectory[0].y_ratio == 412 / 924
        assert command.visual_trajectory[-1].x_ratio == 760 / 1200
        assert command.visual_trajectory[-1].y_ratio == 412 / 924

    asyncio.run(scenario())


def test_enterprise_trusted_challenge_authorization_is_origin_scoped(tmp_path: Path) -> None:
    async def scenario() -> None:
        task = TaskSpec(
            "trusted-challenge",
            "处理企业内部滑块",
            "https://rpa.internal/login",
            ExecutionScope("project"),
            trusted_challenge_origins=("https://rpa.internal",),
            max_security_challenge_attempts=1,
        )
        trusted_driver = RecordingDriver(tmp_path / "trusted")
        trusted_observation = _observation(
            title="企业验证页面",
            summary="请拖动滑块完成验证",
            candidate_risk=DragRiskClass.SECURITY,
            visual_risk=DragRiskClass.SECURITY,
        )
        trusted_observation = replace(
            trusted_observation,
            url="https://rpa.internal/challenge",
        )
        trusted = await ToolExecutor(trusted_driver, task).execute(
            _drag_call(),
            trusted_observation,
        )
        public_driver = RecordingDriver(tmp_path / "public")
        public_observation = _observation(
            title="外部验证页面",
            summary="请拖动滑块完成验证",
            candidate_risk=DragRiskClass.SECURITY,
            visual_risk=DragRiskClass.SECURITY,
        )
        public_observation = replace(public_observation, url="https://public.example/challenge")
        denied = await ToolExecutor(public_driver, task).execute(
            _drag_call(),
            public_observation,
        )

        assert trusted.success is True
        assert trusted.evidence is not None
        assert len(trusted_driver.commands) == 1
        assert denied.success is False
        assert "未授权处理安全挑战" in denied.message
        assert not public_driver.commands

    asyncio.run(scenario())


def test_enterprise_trusted_visual_challenge_uses_multimodal_path(tmp_path: Path) -> None:
    async def scenario() -> None:
        task = TaskSpec(
            "trusted-visual-challenge",
            "处理企业内部视觉滑块",
            "https://rpa.internal/login",
            ExecutionScope("project"),
            trusted_challenge_origins=("https://rpa.internal",),
            max_security_challenge_attempts=1,
            allow_visual_actions=True,
        )
        observation = replace(
            _observation(
                title="企业视觉验证页面",
                summary="请拖动滑块完成验证",
                visual_risk=DragRiskClass.SECURITY,
            ),
            url="https://rpa.internal/challenge",
        )
        driver = RecordingDriver(tmp_path)
        result = await ToolExecutor(
            driver,
            task,
            visual_context_available=True,
        ).execute(_visual_drag_call(), observation)

        assert result.success is True
        assert result.evidence is not None
        assert driver.commands[0].kind is ActionKind.VISUAL_DRAG
        assert driver.commands[0].allow_dynamic_visual_frame is True

    asyncio.run(scenario())


def test_visual_drag_failure_keeps_safe_geometry_audit(tmp_path: Path) -> None:
    class VerificationFailureDriver(RecordingDriver):
        async def execute(self, command: ActionCommand) -> ActionReceipt:
            self.commands.append(command)
            return ActionReceipt(
                command.action_id,
                True,
                True,
                "已执行",
                1.0,
                data={"可视指针反馈": True},
            )

        async def verify(self, condition: object) -> VerificationResult:
            return VerificationResult(False, "页面状态未变化")

    async def scenario() -> None:
        task = TaskSpec(
            "visual-drag-audit",
            "处理视觉滑块",
            "https://example.com/slider",
            ExecutionScope("project"),
            allow_visual_actions=True,
        )
        result = await ToolExecutor(
            VerificationFailureDriver(tmp_path),
            task,
            visual_context_available=True,
        ).execute(_visual_drag_call(), _observation())

        assert result.success is False
        assert result.data["起点视口比例"] == {"x": 0.1, "y": 0.5}
        assert result.data["终点视口比例"] == {"x": 0.8, "y": 0.5}
        assert result.data["轨迹点数"] == 9
        assert result.data["轨迹总时长毫秒"] == 400
        assert result.data["视觉置信度"] == 0.9
        assert result.data["input_dispatched"] is True
        assert result.data["可视指针反馈"] is True
        assert result.data["按下停顿毫秒"] > 0
        vertical_range = result.data["纵向偏移范围"]
        assert vertical_range["最大"] - vertical_range["最小"] > 0.0001

    asyncio.run(scenario())


def test_visual_drag_rejects_postcondition_already_true_before_action(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        task = TaskSpec(
            "visual-pre-satisfied",
            "处理视觉滑块",
            "https://example.com/slider",
            ExecutionScope("project"),
            allow_visual_actions=True,
        )
        call = _visual_drag_call()
        call.arguments["expect_value"] = "普通业务滑块"

        result = await ToolExecutor(
            driver,
            task,
            visual_context_available=True,
        ).execute(call, _observation())

        assert result.success is False
        assert "动作前已经满足" in result.message
        assert not driver.commands

    asyncio.run(scenario())


def test_visual_drag_calibrates_start_outside_code_inferred_handle(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        task = TaskSpec(
            "visual-geometry-guard",
            "处理视觉滑块",
            "https://example.com/slider",
            ExecutionScope("project"),
            allow_visual_actions=True,
            allow_security_challenge=True,
        )
        observation = replace(
            _observation(visual_risk=DragRiskClass.SECURITY),
            candidates=(
                CandidateTarget(
                    "track",
                    "button",
                    "滑块轨道",
                    "",
                    0.82,
                    ("指针候选",),
                    LocatorRecipe("pointer_css"),
                    box=BoundingBox(460, 402, 280, 20),
                    drag_risk=DragRiskClass.UNKNOWN,
                ),
            ),
            metadata={"CSS视口": {"width": 1200, "height": 924}},
        )
        call = _visual_drag_call(security_challenge=True)
        call.arguments.update(
            {
                "start_x_ratio": 0.41,
                "start_y_ratio": 0.45,
                "end_x_ratio": 0.64,
                "end_y_ratio": 0.45,
            }
        )

        result = await ToolExecutor(
            driver,
            task,
            visual_context_available=True,
        ).execute(call, observation)

        assert result.success is True
        command = driver.commands[0]
        assert command.visual_trajectory[0].x_ratio == 440 / 1200
        assert command.visual_trajectory[0].y_ratio == 412 / 924
        assert command.visual_trajectory[-1].x_ratio == 760 / 1200
        assert command.visual_trajectory[-1].y_ratio == 412 / 924

    asyncio.run(scenario())


def test_visual_drag_requires_task_permission_and_current_fingerprint(tmp_path: Path) -> None:
    async def scenario() -> None:
        denied_driver = RecordingDriver(tmp_path / "denied")
        denied = await ToolExecutor(
            denied_driver,
            TaskSpec(
                "visual-denied",
                "处理视觉滑块",
                "https://example.com/slider",
                ExecutionScope("project"),
            ),
        ).execute(_visual_drag_call(), _observation())
        assert denied.success is False
        assert "未授权视觉坐标动作" in denied.message

        allowed_driver = RecordingDriver(tmp_path / "allowed")
        executor = ToolExecutor(
            allowed_driver,
            TaskSpec(
                "visual-allowed",
                "处理视觉滑块",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_visual_actions=True,
            ),
            visual_context_available=True,
        )
        stale = await executor.execute(_visual_drag_call(fingerprint="stale"), _observation())
        current = await executor.execute(_visual_drag_call(), _observation())

        assert stale.success is False
        assert "页面观察已经失效" in stale.message
        assert current.success is True
        assert current.plan_step is None
        assert allowed_driver.commands[0].kind is ActionKind.VISUAL_DRAG

    asyncio.run(scenario())


def test_visual_drag_requires_current_multimodal_context(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = RecordingDriver(tmp_path)
        executor = ToolExecutor(
            driver,
            TaskSpec(
                "visual-without-image",
                "处理视觉滑块",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_visual_actions=True,
            ),
        )

        result = await executor.execute(_visual_drag_call(), _observation())

        assert result.success is False
        assert "模型图片输入" in result.message
        assert not driver.commands

    asyncio.run(scenario())


def test_visual_drag_security_risk_requires_authorization_and_unique_strategy(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        challenge_observation = _observation(
            title="滑动验证页面",
            summary="请完成人机验证",
            visual_risk=DragRiskClass.SECURITY,
        )
        denied_driver = RecordingDriver(tmp_path / "denied")
        denied = await ToolExecutor(
            denied_driver,
            TaskSpec(
                "visual-challenge-denied",
                "处理视觉验证",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_visual_actions=True,
            ),
            visual_context_available=True,
        ).execute(_visual_drag_call(), challenge_observation)

        allowed_driver = RecordingDriver(tmp_path / "allowed")
        executor = ToolExecutor(
            allowed_driver,
            TaskSpec(
                "visual-challenge-allowed",
                "处理已授权视觉验证",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_visual_actions=True,
                allow_security_challenge=True,
                max_security_challenge_attempts=1,
            ),
            visual_context_available=True,
        )
        first = await executor.execute(
            _visual_drag_call(security_challenge=True),
            challenge_observation,
        )
        second = await executor.execute(
            _visual_drag_call(security_challenge=True),
            challenge_observation,
        )

        assert denied.success is False
        assert "未授权处理安全挑战" in denied.message
        assert first.success is True
        assert first.evidence is not None
        assert allowed_driver.commands[0].allow_dynamic_visual_frame is True
        assert second.success is False
        assert "等价拖拽策略" in second.message
        assert second.counts_as_action is False
        assert len(allowed_driver.commands) == 1
        assert allowed_driver.evidence_count == 1

    asyncio.run(scenario())


def test_visual_drag_rejects_repeated_strategy_and_resets_for_new_challenge(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        observation = _observation(
            title="滑动验证页面",
            summary="请完成人机验证",
            visual_risk=DragRiskClass.SECURITY,
        )
        driver = RecordingDriver(tmp_path)
        executor = ToolExecutor(
            driver,
            TaskSpec(
                "visual-challenge-strategy",
                "处理已授权视觉验证",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_visual_actions=True,
                allow_security_challenge=True,
                max_security_challenge_attempts=3,
            ),
            visual_context_available=True,
        )

        first = await executor.execute(
            _visual_drag_call(security_challenge=True, motion_profile="balanced"),
            observation,
        )
        repeated = await executor.execute(
            _visual_drag_call(security_challenge=True, motion_profile="balanced"),
            observation,
        )
        switched = await executor.execute(
            _visual_drag_call(security_challenge=True, motion_profile="steady"),
            observation,
        )

        assert first.success is True
        assert repeated.success is False
        assert repeated.counts_as_action is False
        assert repeated.data["reason"] == "repeated_challenge_strategy"
        assert switched.success is True
        assert executor.high_risk_drag_attempts == 2
        assert len(executor.security_challenge_drag_strategies) == 2
        assert all(
            len(signature) == 64 for signature in executor.security_challenge_drag_strategies
        )
        assert len(driver.commands) == 2

        executor.clear_security_challenge()
        new_challenge = await executor.execute(
            _visual_drag_call(security_challenge=True, motion_profile="balanced"),
            observation,
        )

        assert new_challenge.success is True
        assert executor.high_risk_drag_attempts == 1
        assert len(executor.security_challenge_drag_strategies) == 1
        assert len(driver.commands) == 3

    asyncio.run(scenario())


def test_visual_drag_allows_same_motion_profile_after_screenshot_changes(tmp_path: Path) -> None:
    async def scenario() -> None:
        observation = _observation(
            title="滑动验证页面",
            summary="请完成人机验证",
            visual_risk=DragRiskClass.SECURITY,
        )
        driver = RecordingDriver(tmp_path)
        executor = ToolExecutor(
            driver,
            TaskSpec(
                "visual-challenge-new-screenshot",
                "处理已授权视觉验证",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_visual_actions=True,
                allow_security_challenge=True,
            ),
            visual_context_available=True,
        )

        first = await executor.execute(
            _visual_drag_call(security_challenge=True, motion_profile="balanced"),
            observation,
        )
        refreshed_call = replace(
            _visual_drag_call(security_challenge=True, motion_profile="balanced"),
            call_id="visual-drag-refreshed",
            arguments={
                **_visual_drag_call(
                    security_challenge=True,
                    motion_profile="balanced",
                ).arguments,
                "screenshot_fingerprint": "refreshed-screenshot",
            },
        )
        refreshed = await executor.execute(refreshed_call, observation)

        assert first.success is True
        assert refreshed.success is True
        assert len(driver.commands) == 2
        assert len(executor.security_challenge_drag_strategies) == 2

    asyncio.run(scenario())


def test_restored_idle_challenge_discards_legacy_attempts_and_histories(tmp_path: Path) -> None:
    executor = ToolExecutor(
        RecordingDriver(tmp_path),
        TaskSpec(
            "legacy-idle-challenge",
            "继续查询订单",
            "https://example.com/order",
            ExecutionScope("project"),
            allow_security_challenge=True,
            max_security_challenge_attempts=3,
        ),
    )

    executor.restore_security_challenge(
        attempts=3,
        phase="idle",
        drag_strategies=("track:balanced",),
        text_signatures=("a" * 64,),
    )

    assert executor.high_risk_drag_attempts == 0
    assert executor.security_challenge_active is False
    assert executor.security_challenge_phase == "idle"
    assert executor.security_challenge_drag_strategies == []
    assert executor.security_challenge_text_signatures == []


def test_visual_stale_frame_before_input_does_not_consume_high_risk_budget(
    tmp_path: Path,
) -> None:
    class StaleThenSuccessDriver(RecordingDriver):
        async def execute(self, command: ActionCommand) -> ActionReceipt:
            self.commands.append(command)
            if len(self.commands) == 1:
                return ActionReceipt(
                    command.action_id,
                    False,
                    True,
                    "视觉拖拽绑定的页面观察已经变化",
                    1.0,
                    {"input_dispatched": False},
                )
            return ActionReceipt(
                command.action_id,
                True,
                True,
                "已执行",
                1.0,
                {"input_dispatched": True},
            )

    async def scenario() -> None:
        observation = _observation(visual_risk=DragRiskClass.SECURITY)
        driver = StaleThenSuccessDriver(tmp_path)
        executor = ToolExecutor(
            driver,
            TaskSpec(
                "stale-before-input",
                "处理已授权动态验证",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_visual_actions=True,
                allow_security_challenge=True,
                max_security_challenge_attempts=1,
            ),
            visual_context_available=True,
        )

        first = await executor.execute(_visual_drag_call(security_challenge=True), observation)
        second = await executor.execute(_visual_drag_call(security_challenge=True), observation)

        assert first.success is False
        assert second.success is True
        assert len(driver.commands) == 2
        assert driver.evidence_count == 2

    asyncio.run(scenario())


def test_unknown_visual_drag_is_fail_closed_without_explicit_permission(tmp_path: Path) -> None:
    async def scenario() -> None:
        unknown_observation = _observation(visual_risk=DragRiskClass.UNKNOWN)
        denied_driver = RecordingDriver(tmp_path / "denied")
        denied = await ToolExecutor(
            denied_driver,
            TaskSpec(
                "visual-unknown-denied",
                "处理无法分类的视觉滑块",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_visual_actions=True,
            ),
            visual_context_available=True,
        ).execute(_visual_drag_call(), unknown_observation)

        allowed_driver = RecordingDriver(tmp_path / "allowed")
        allowed = await ToolExecutor(
            allowed_driver,
            TaskSpec(
                "visual-unknown-allowed",
                "处理已授权的未知视觉滑块",
                "https://example.com/slider",
                ExecutionScope("project"),
                allow_visual_actions=True,
                allow_unknown_visual_drag=True,
            ),
            visual_context_available=True,
        ).execute(_visual_drag_call(), unknown_observation)

        assert denied.success is False
        assert "风险无法确认" in denied.message
        assert allowed.success is True
        assert allowed.evidence is not None
        assert allowed.evidence.kind == "high_risk_drag_before"
        assert allowed_driver.evidence_count == 1

    asyncio.run(scenario())

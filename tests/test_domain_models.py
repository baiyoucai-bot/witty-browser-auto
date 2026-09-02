from __future__ import annotations

import pytest

from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionKind,
    DragPoint,
    DragRiskClass,
    ExecutionScope,
    TaskSpec,
    VisualDragPoint,
)


def test_task_spec_rejects_empty_goal() -> None:
    with pytest.raises(ValueError, match="任务目标不能为空"):
        TaskSpec(
            task_id="task-1",
            goal=" ",
            start_url="https://example.com",
            scope=ExecutionScope(project_id="demo"),
        )


def test_navigation_requires_url() -> None:
    with pytest.raises(ValueError, match="导航动作必须提供 URL"):
        ActionCommand(action_id="a1", kind=ActionKind.NAVIGATE)


def test_input_requires_target_and_value() -> None:
    with pytest.raises(ValueError, match="必须提供目标区域"):
        ActionCommand(action_id="a1", kind=ActionKind.INPUT_TEXT, value="测试")

    with pytest.raises(ValueError, match="必须提供文本"):
        ActionCommand(action_id="a2", kind=ActionKind.INPUT_TEXT, target_id="target-1")


def test_drag_requires_bounded_non_idempotent_trajectory() -> None:
    with pytest.raises(ValueError, match="2 到 120"):
        ActionCommand(
            action_id="drag-short",
            kind=ActionKind.DRAG,
            target_id="slider",
            trajectory=(DragPoint(0, 0),),
        )

    with pytest.raises(ValueError, match="目标区域中心"):
        ActionCommand(
            action_id="drag-start",
            kind=ActionKind.DRAG,
            target_id="slider",
            trajectory=(DragPoint(1, 0), DragPoint(100, 0)),
        )

    with pytest.raises(ValueError, match="非幂等"):
        ActionCommand(
            action_id="drag-idempotent",
            kind=ActionKind.DRAG,
            target_id="slider",
            trajectory=(DragPoint(0, 0), DragPoint(100, 0)),
            idempotent=True,
        )


def test_task_accepts_legacy_positive_challenge_attempt_hint_for_compatibility() -> None:
    task = TaskSpec(
        task_id="challenge-audit-hint",
        goal="处理已授权滑块",
        start_url="https://example.com",
        scope=ExecutionScope("project"),
        max_security_challenge_attempts=4,
    )

    assert task.max_security_challenge_attempts == 4


def test_task_rejects_unknown_visual_drag_without_visual_permission() -> None:
    with pytest.raises(ValueError, match="必须先授权视觉坐标动作"):
        TaskSpec(
            task_id="unknown-visual",
            goal="处理未知视觉滑块",
            start_url="https://example.com",
            scope=ExecutionScope("project"),
            allow_unknown_visual_drag=True,
        )


def test_drag_requires_execution_risk_classification() -> None:
    with pytest.raises(ValueError, match="风险分类"):
        ActionCommand(
            action_id="drag-risk-missing",
            kind=ActionKind.DRAG,
            target_id="slider",
            trajectory=(DragPoint(0, 0), DragPoint(100, 0)),
        )

    command = ActionCommand(
        action_id="drag-business",
        kind=ActionKind.DRAG,
        target_id="slider",
        trajectory=(DragPoint(0, 0), DragPoint(100, 0)),
        drag_risk=DragRiskClass.BUSINESS,
    )
    assert command.drag_risk is DragRiskClass.BUSINESS


def test_visual_drag_requires_fingerprint_and_high_confidence() -> None:
    trajectory = (VisualDragPoint(0.1, 0.5, 0), VisualDragPoint(0.8, 0.5, 100))

    with pytest.raises(ValueError, match="观察指纹"):
        ActionCommand(
            action_id="visual-no-fingerprint",
            kind=ActionKind.VISUAL_DRAG,
            visual_trajectory=trajectory,
            visual_confidence=0.9,
        )

    with pytest.raises(ValueError, match=r"0\.8 到 1"):
        ActionCommand(
            action_id="visual-low-confidence",
            kind=ActionKind.VISUAL_DRAG,
            visual_trajectory=trajectory,
            observation_fingerprint="fingerprint",
            screenshot_fingerprint="screenshot",
            visual_confidence=0.79,
        )

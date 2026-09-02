from __future__ import annotations

from itertools import pairwise

import pytest

from witty_browser_auto.agent.drag_trajectory import (
    VISUAL_DRAG_MOTION_PROFILES,
    build_human_visual_drag_trajectory,
)


def test_human_visual_drag_is_bounded_reproducible_and_not_mechanical() -> None:
    points = build_human_visual_drag_trajectory(
        start_x_ratio=0.38,
        start_y_ratio=0.45,
        end_x_ratio=0.65,
        end_y_ratio=0.45,
        duration_ms=1500,
        steps=60,
        seed="action-a",
    )

    assert len(points) == 60
    assert (points[0].x_ratio, points[0].y_ratio) == (0.38, 0.45)
    assert (points[-1].x_ratio, points[-1].y_ratio) == (0.65, 0.45)
    assert sum(point.delay_ms for point in points) == 1500
    assert 40 <= points[0].delay_ms <= 160
    assert all(0 <= point.delay_ms <= 200 for point in points)
    assert all(left.x_ratio <= right.x_ratio for left, right in pairwise(points))
    assert any(abs(point.y_ratio - 0.45) > 0.0001 for point in points[1:-1])
    assert len({point.delay_ms for point in points[1:]}) > 2
    assert points == build_human_visual_drag_trajectory(
        start_x_ratio=0.38,
        start_y_ratio=0.45,
        end_x_ratio=0.65,
        end_y_ratio=0.45,
        duration_ms=1500,
        steps=60,
        seed="action-a",
    )
    assert points != build_human_visual_drag_trajectory(
        start_x_ratio=0.38,
        start_y_ratio=0.45,
        end_x_ratio=0.65,
        end_y_ratio=0.45,
        duration_ms=1500,
        steps=60,
        seed="action-b",
    )


@pytest.mark.parametrize(
    ("duration_ms", "steps"),
    ((99, 20), (5001, 20), (400, 1), (400, 121), (1000, 2)),
)
def test_human_visual_drag_rejects_invalid_budget(duration_ms: int, steps: int) -> None:
    with pytest.raises(ValueError):
        build_human_visual_drag_trajectory(
            start_x_ratio=0.1,
            start_y_ratio=0.5,
            end_x_ratio=0.9,
            end_y_ratio=0.5,
            duration_ms=duration_ms,
            steps=steps,
            seed="invalid",
        )


def test_visual_drag_motion_profiles_are_distinct_bounded_and_reproducible() -> None:
    trajectories = {
        profile: build_human_visual_drag_trajectory(
            start_x_ratio=0.38,
            start_y_ratio=0.45,
            end_x_ratio=0.65,
            end_y_ratio=0.45,
            duration_ms=1500,
            steps=60,
            seed="same-challenge",
            motion_profile=profile,
        )
        for profile in VISUAL_DRAG_MOTION_PROFILES
    }

    assert len(set(trajectories.values())) == len(VISUAL_DRAG_MOTION_PROFILES)
    for profile, points in trajectories.items():
        assert points == build_human_visual_drag_trajectory(
            start_x_ratio=0.38,
            start_y_ratio=0.45,
            end_x_ratio=0.65,
            end_y_ratio=0.45,
            duration_ms=1500,
            steps=60,
            seed="same-challenge",
            motion_profile=profile,
        )
        assert all(0 <= point.x_ratio <= 1 for point in points)
        assert all(0 <= point.y_ratio <= 1 for point in points)
        assert all(left.x_ratio <= right.x_ratio for left, right in pairwise(points))
        assert sum(point.delay_ms for point in points) == 1500


def test_visual_drag_rejects_unknown_motion_profile() -> None:
    with pytest.raises(ValueError, match="运动策略无效"):
        build_human_visual_drag_trajectory(
            start_x_ratio=0.38,
            start_y_ratio=0.45,
            end_x_ratio=0.65,
            end_y_ratio=0.45,
            duration_ms=1500,
            steps=60,
            seed="unknown-profile",
            motion_profile="duplicate",
        )

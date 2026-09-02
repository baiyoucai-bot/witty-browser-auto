from __future__ import annotations

from dataclasses import replace

from witty_browser_auto.agent.visual_geometry import (
    challenge_refresh_condition,
    challenge_refresh_target_ids,
    security_drag_geometry_error,
    security_drag_geometry_ratios,
)
from witty_browser_auto.domain.models import (
    BoundingBox,
    CandidateTarget,
    DragRiskClass,
    LocatorRecipe,
    Observation,
)


def _challenge_observation() -> Observation:
    return Observation(
        "surface",
        "https://example.com/challenge",
        "滑动验证页面",
        1,
        "fingerprint",
        "请完成验证",
        (
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
        visual_drag_risk=DragRiskClass.SECURITY,
        metadata={"CSS视口": {"width": 1200, "height": 924}},
    )


def test_security_drag_geometry_rejects_start_outside_inferred_handle() -> None:
    error = security_drag_geometry_error(
        {
            "start_x_ratio": 0.41,
            "start_y_ratio": 0.45,
            "end_x_ratio": 0.64,
            "end_y_ratio": 0.45,
        },
        _challenge_observation(),
    )

    assert error is not None
    assert "起点未落入" in error
    assert "0.3667" in error
    assert "0.6333" in error


def test_security_drag_geometry_accepts_point_inside_inferred_handle() -> None:
    assert (
        security_drag_geometry_error(
            {
                "start_x_ratio": 0.37,
                "start_y_ratio": 0.446,
                "end_x_ratio": 0.635,
                "end_y_ratio": 0.446,
            },
            _challenge_observation(),
        )
        is None
    )


def test_security_drag_geometry_returns_exact_track_centers() -> None:
    assert security_drag_geometry_ratios(_challenge_observation()) == (
        440 / 1200,
        412 / 924,
        760 / 1200,
        412 / 924,
    )


def test_security_drag_geometry_validation_ignores_non_slider_bars() -> None:
    observation = _challenge_observation()
    decorative_bar = CandidateTarget(
        "decorative",
        "div",
        "progress-decoration",
        "",
        0.1,
        ("指针候选",),
        LocatorRecipe("pointer_css"),
        box=BoundingBox(100, 360, 500, 80),
        drag_risk=DragRiskClass.UNKNOWN,
    )
    observation = replace(
        observation,
        candidates=(decorative_bar, *observation.candidates),
    )

    error = security_drag_geometry_error(
        {
            "start_x_ratio": 0.41,
            "start_y_ratio": 0.446,
            "end_x_ratio": 0.635,
            "end_y_ratio": 0.446,
        },
        observation,
    )

    assert error is not None
    assert "起点未落入" in error


def test_failed_challenge_exposes_refresh_but_not_drag_geometry() -> None:
    observation = Observation(
        "surface",
        "https://example.com/challenge",
        "滑动验证页面",
        2,
        "failed",
        "验证失败，请刷新",
        (
            CandidateTarget(
                "refresh",
                "button",
                "captcha-sliding-refresh",
                "\ue685",
                0.82,
                ("指针候选",),
                LocatorRecipe("pointer_css"),
                box=BoundingBox(820, 548, 14, 14),
            ),
        ),
        visual_drag_risk=DragRiskClass.SECURITY,
        metadata={"CSS视口": {"width": 1521, "height": 1284}},
    )

    assert challenge_refresh_target_ids(observation) == ("refresh",)
    condition = challenge_refresh_condition(observation.candidates[0], observation)
    assert condition is not None
    assert condition.kind == "challenge_refreshed"
    assert condition.value == observation.fingerprint
    assert security_drag_geometry_ratios(observation) is None

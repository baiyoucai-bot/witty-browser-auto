"""用页面公开几何信息校准高风险视觉拖拽。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from witty_browser_auto.domain.models import (
    BoundingBox,
    CandidateTarget,
    DragRiskClass,
    ExpectedCondition,
    Observation,
)

_CHALLENGE_MARKERS = (
    "验证您是真人",
    "请按住滑块",
    "拖动到最右边",
    "滑动验证",
    "人机验证",
    "真人验证",
    "安全验证",
)
_REFRESH_TERMS = ("刷新", "重试", "重新验证", "retry", "refresh")
_SLIDER_TERMS = ("滑块", "滑动", "拖动", "按住", "slider", "slide")


def _ratio(arguments: Mapping[str, Any], key: str) -> float | None:
    value = arguments.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _security_drag_track_boxes(observation: Observation) -> tuple[BoundingBox, ...]:
    return tuple(
        candidate.box
        for candidate in observation.candidates
        if candidate.box is not None
        and candidate.box.height >= 8
        and candidate.box.width >= candidate.box.height * 4
        and (
            candidate.drag_risk is DragRiskClass.SECURITY
            or any(
                term in f"{candidate.name} {candidate.text}".casefold() for term in _SLIDER_TERMS
            )
        )
    )


def security_drag_geometry_ratios(
    observation: Observation,
) -> tuple[float, float, float, float] | None:
    """从唯一细长轨道推导手柄中心和轨道终点的精确视口比例。"""

    if observation.visual_drag_risk is not DragRiskClass.SECURITY:
        return None
    viewport = observation.metadata.get("CSS视口")
    if not isinstance(viewport, Mapping):
        return None
    width, height = viewport.get("width"), viewport.get("height")
    if not isinstance(width, int | float) or not isinstance(height, int | float):
        return None
    if width <= 0 or height <= 0:
        return None
    tracks = _security_drag_track_boxes(observation)
    if len(tracks) != 1:
        return None
    track = tracks[0]
    center_y = (track.y + track.height / 2) / height
    start_x = (track.x - track.height) / width
    end_x = (track.x + track.width + track.height) / width
    return start_x, center_y, end_x, center_y


def security_drag_geometry_error(
    arguments: Mapping[str, Any],
    observation: Observation,
) -> str | None:
    """保留模型坐标诊断；执行器会直接吸附到同一组精确几何。"""

    inferred = security_drag_geometry_ratios(observation)
    if inferred is None:
        return None
    viewport = observation.metadata["CSS视口"]
    width, height = float(viewport["width"]), float(viewport["height"])
    tracks = _security_drag_track_boxes(observation)
    track = tracks[0]
    start_x = _ratio(arguments, "start_x_ratio")
    start_y = _ratio(arguments, "start_y_ratio")
    end_x = _ratio(arguments, "end_x_ratio")
    end_y = _ratio(arguments, "end_y_ratio")
    if None in {start_x, start_y, end_x, end_y}:
        return None
    inferred_start_x, inferred_y, inferred_end_x, _ = inferred
    start_ok = (
        abs(start_x - inferred_start_x) <= track.height / width
        and abs(start_y - inferred_y) <= track.height / height
    )
    end_ok = (
        abs(end_x - inferred_end_x) <= track.height / width * 2
        and abs(end_y - inferred_y) <= track.height / height
    )
    if start_ok and end_ok:
        return None
    problem = "起点未落入代码推断的滑块手柄区域" if not start_ok else "终点未到达轨道末端"
    return (
        f"代码几何校验失败：{problem}；请重新观察并参考建议比例："
        f"起点({inferred_start_x:.4f}, {inferred_y:.4f})，"
        f"终点({inferred_end_x:.4f}, {inferred_y:.4f})"
    )


def challenge_clearance_condition(observation: Observation) -> ExpectedCondition:
    summary = f"{observation.title}\n{observation.summary}"
    marker = next((term for term in _CHALLENGE_MARKERS if term in summary), "验证")
    return ExpectedCondition("challenge_cleared", marker, timeout_seconds=3)


def drag_challenge_condition(
    action_name: str,
    target_id: object,
    observation: Observation,
) -> ExpectedCondition | None:
    if action_name == "visual_drag" and observation.visual_drag_risk is DragRiskClass.SECURITY:
        return challenge_clearance_condition(observation)
    if action_name == "drag" and any(
        item.target_id == target_id and item.drag_risk is DragRiskClass.SECURITY
        for item in observation.candidates
    ):
        return challenge_clearance_condition(observation)
    return None


def challenge_refresh_condition(
    candidate: CandidateTarget,
    observation: Observation,
) -> ExpectedCondition | None:
    text = f"{candidate.name} {candidate.text}".casefold()
    if observation.visual_drag_risk is DragRiskClass.SECURITY and any(
        term in text for term in _REFRESH_TERMS
    ):
        return ExpectedCondition("challenge_refreshed", observation.fingerprint, timeout_seconds=3)
    return None


def challenge_refresh_target_ids(observation: Observation) -> tuple[str, ...]:
    """返回当前失败页中可安全刷新挑战的精确候选，供工具 schema 绑定。"""

    return tuple(
        candidate.target_id
        for candidate in observation.candidates
        if challenge_refresh_condition(candidate, observation) is not None
    )

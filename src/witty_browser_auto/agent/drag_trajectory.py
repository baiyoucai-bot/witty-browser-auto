"""生成有界、可复现且避免机械直线特征的视觉拖拽轨迹。"""

from __future__ import annotations

import math
import random

from witty_browser_auto.domain.models import DragPoint, VisualDragPoint

VISUAL_DRAG_MOTION_PROFILES = ("balanced", "steady", "ease_out", "hesitant")


def build_drag_trajectory(
    *,
    end_dx: float,
    end_dy: float,
    duration_ms: int,
    steps: int,
) -> tuple[DragPoint, ...]:
    """为已语义定位的目标生成有界缓动轨迹。"""

    if not 100 <= duration_ms <= 5000:
        raise ValueError("拖拽持续时间必须在 100 到 5000 毫秒之间")
    if not 2 <= steps <= 120:
        raise ValueError("拖拽轨迹点数必须在 2 到 120 之间")
    move_count = steps - 1
    if duration_ms > move_count * 200:
        raise ValueError("拖拽轨迹点太少，单点延时不能超过 200 毫秒")
    base_delay, remainder = divmod(duration_ms, move_count)
    points = [DragPoint(0, 0, 0)]
    for index in range(1, steps):
        progress = index / move_count
        eased = (1 - math.cos(math.pi * progress)) / 2
        delay = base_delay + (1 if index <= remainder else 0)
        points.append(DragPoint(end_dx * eased, end_dy * eased, delay))
    return tuple(points)


def _natural_delays(duration_ms: int, steps: int, rng: random.Random) -> list[int]:
    if not 100 <= duration_ms <= 5000:
        raise ValueError("拖拽持续时间必须在 100 到 5000 毫秒之间")
    if not 2 <= steps <= 120:
        raise ValueError("拖拽轨迹点数必须在 2 到 120 之间")
    move_count = steps - 1
    if duration_ms > move_count * 200:
        raise ValueError("拖拽轨迹点太少，单点延时不能超过 200 毫秒")

    # 第一个延时表示鼠标按下后的短暂停顿；其余时间分配给移动事件。
    hold_max = min(160, max(0, duration_ms - move_count))
    hold_min = min(60, hold_max)
    hold = rng.randint(hold_min, hold_max) if hold_max else 0
    base, remainder = divmod(duration_ms - hold, move_count)
    moves = [base + (index < remainder) for index in range(move_count)]
    for index in range(move_count - 1):
        shift = rng.randint(-min(6, moves[index + 1]), min(6, moves[index]))
        if moves[index] - shift <= 200 and moves[index + 1] + shift <= 200:
            moves[index] -= shift
            moves[index + 1] += shift
    return [hold, *moves]


def build_human_visual_drag_trajectory(
    *,
    start_x_ratio: float,
    start_y_ratio: float,
    end_x_ratio: float,
    end_y_ratio: float,
    duration_ms: int,
    steps: int,
    seed: str,
    motion_profile: str = "balanced",
) -> tuple[VisualDragPoint, ...]:
    """按模型选择的运动策略生成有界、可复现且可审计的轨迹。"""
    if motion_profile not in VISUAL_DRAG_MOTION_PROFILES:
        raise ValueError("视觉拖拽运动策略无效")
    rng = random.Random(seed)
    delays = _natural_delays(duration_ms, steps, rng)
    move_count = steps - 1
    gamma = rng.uniform(1.35, 2.05)
    ease_out_gamma = rng.uniform(1.8, 2.2)
    base_amplitude = min(0.0025, max(0.0008, abs(end_x_ratio - start_x_ratio) * 0.008))
    amplitude = base_amplitude * (0.45 if motion_profile == "steady" else 1.0)
    curve = rng.uniform(-amplitude, amplitude)
    ripple = rng.uniform(0.15, 0.35) * amplitude
    points: list[VisualDragPoint] = []
    for index in range(steps):
        if index in {0, move_count}:
            x_ratio = start_x_ratio if index == 0 else end_x_ratio
            y_ratio = start_y_ratio if index == 0 else end_y_ratio
        else:
            progress = index / move_count
            if motion_profile == "steady":
                eased = progress
            elif motion_profile == "ease_out":
                eased = 1 - (1 - progress) ** ease_out_gamma
            elif motion_profile == "hesitant":
                # 中段短暂停留后继续推进，保持单调且最终落点不变。
                if progress < 0.42:
                    eased = progress / 0.42 * 0.3
                elif progress < 0.58:
                    eased = 0.3 + (progress - 0.42) / 0.16 * 0.035
                else:
                    eased = 0.335 + (progress - 0.58) / 0.42 * 0.665
            else:
                powered = progress**gamma
                eased = powered / (powered + (1 - progress) ** gamma)
            x_ratio = start_x_ratio + (end_x_ratio - start_x_ratio) * eased
            base_y = start_y_ratio + (end_y_ratio - start_y_ratio) * eased
            deviation = math.sin(math.pi * progress) * (
                curve + ripple * math.sin(5 * math.pi * progress)
            )
            y_ratio = min(1.0, max(0.0, base_y + deviation))
        points.append(VisualDragPoint(x_ratio, y_ratio, delays[index]))
    return tuple(points)

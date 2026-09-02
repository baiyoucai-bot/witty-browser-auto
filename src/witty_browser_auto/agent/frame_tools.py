"""iframe 帧清单工具：把页面的帧结构暴露成可用于定位器的 frame_id。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from witty_browser_auto.domain.models import ModelToolCall
from witty_browser_auto.domain.protocols import AutomationDriver, FrameInspectionProvider
from witty_browser_auto.toolkit.catalog import FRAME_TOOLS, names_of, schemas_of

FRAME_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = schemas_of(FRAME_TOOLS)
FRAME_TOOL_NAMES = names_of(FRAME_TOOLS)

_MAX_FRAMES_RETURNED = 50


@dataclass(frozen=True, slots=True)
class FrameToolOutcome:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


def frame_inspection_available(driver: AutomationDriver) -> bool:
    return isinstance(driver, FrameInspectionProvider)


async def execute_frame_tool(
    call: ModelToolCall,
    driver: AutomationDriver,
) -> FrameToolOutcome:
    if call.name not in FRAME_TOOL_NAMES:
        raise ValueError(f"未知帧工具：{call.name}")
    if not isinstance(driver, FrameInspectionProvider):
        return FrameToolOutcome(False, "当前浏览器表面没有 iframe 帧列举能力")
    if call.arguments:
        raise ValueError("帧列举不接受任何参数")
    frames = await driver.list_frames()
    child_frames = [frame for frame in frames if not frame.get("is_main")]
    return FrameToolOutcome(
        True,
        "页面帧结构已只读列举",
        {
            "frames": frames[:_MAX_FRAMES_RETURNED],
            "frame_count": len(frames),
            "child_frame_count": len(child_frames),
            "cross_origin_frame_count": sum(
                1 for frame in child_frames if frame.get("cross_origin")
            ),
        },
    )

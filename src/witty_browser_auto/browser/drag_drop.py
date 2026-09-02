"""元素到元素的拖放，自动在 HTML5 原生通道与鼠标通道之间择路。

真实 Chrome 探测确认了这里必须分两条通道：同一串 `Input.dispatchMouseEvent`
对 `mousedown/mousemove/mouseup` 型拖放也就是 sortable 类库那种完全有效，但对
`draggable="true"` 的 HTML5 原生拖放**只触发 dragstart，drop 永远不发生**。
也就是说单靠鼠标事件做元素到元素拖放，在一半的站点上会"看着像做了、其实没放下"。

择路方式是先开 `Input.setInterceptDrags`，按下并移动一小段后看浏览器是否截获到拖拽
数据：截获到说明这是原生拖放，改用 `Input.dispatchDragEvent` 把 dragEnter/dragOver/drop
补到目标点；没截获说明是鼠标型，继续把鼠标移到目标并释放。两条路都不需要调用方预先
知道页面用的是哪种实现。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# 按下后先挪一小段以触发拖拽判定；太小浏览器不认为这是拖拽。
_DRAG_KICKOFF_PX = 12.0
_INTERCEPT_WAIT_SECONDS = 1.5
_MIN_STEPS = 4
_MAX_STEPS = 60


class DragSession(Protocol):
    session_id: str
    connection: Any

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...


async def drag_between_points(
    session: DragSession,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    steps: int = 12,
    step_delay_ms: int = 16,
) -> dict[str, Any]:
    """把起点拖到终点，自动识别原生拖放还是鼠标拖放。"""

    steps = max(_MIN_STEPS, min(_MAX_STEPS, steps))
    start_x, start_y = start
    end_x, end_y = end

    intercepted: dict[str, Any] = {}
    captured = asyncio.Event()

    def on_drag_intercepted(event: Any) -> None:
        data = getattr(event, "params", {}).get("data")
        if data is not None and not captured.is_set():
            intercepted["data"] = data
            captured.set()

    interception_enabled = False
    unsubscribe = None
    try:
        await session.call("Input.setInterceptDrags", {"enabled": True})
        interception_enabled = True
        unsubscribe = session.connection.subscribe(
            "Input.dragIntercepted", on_drag_intercepted, session_id=session.session_id
        )
    except Exception as exc:
        # 拿不到原生通道时退回纯鼠标，总比整个动作失败好。
        logger.info(
            "无法启用原生拖放截获，改用鼠标通道",
            extra={"exception_type": type(exc).__name__},
        )

    try:
        await _mouse(session, "mouseMoved", start_x, start_y, buttons=0)
        await _mouse(session, "mousePressed", start_x, start_y, buttons=1, click_count=1)

        # 先挪一小段触发拖拽判定，再决定走哪条通道。
        kick_x, kick_y = _kickoff_point(start_x, start_y, end_x, end_y)
        await _mouse(session, "mouseMoved", kick_x, kick_y, buttons=1)

        if interception_enabled:
            try:
                await asyncio.wait_for(captured.wait(), _INTERCEPT_WAIT_SECONDS)
            except TimeoutError:
                pass

        if intercepted:
            return await _finish_native(
                session, intercepted["data"], end_x, end_y, steps, step_delay_ms
            )
        return await _finish_mouse(session, kick_x, kick_y, end_x, end_y, steps, step_delay_ms)
    finally:
        if unsubscribe is not None:
            unsubscribe()
        if interception_enabled:
            try:
                await session.call("Input.setInterceptDrags", {"enabled": False})
            except Exception:
                logger.warning("关闭原生拖放截获失败，后续拖拽可能仍被截获")


async def _finish_native(
    session: DragSession,
    data: Any,
    end_x: float,
    end_y: float,
    steps: int,
    step_delay_ms: int,
) -> dict[str, Any]:
    """原生拖放：浏览器已经接管指针，剩下的必须用拖拽事件补齐。"""

    await session.call(
        "Input.dispatchDragEvent", {"type": "dragEnter", "x": end_x, "y": end_y, "data": data}
    )
    for _ in range(max(1, steps // 4)):
        await session.call(
            "Input.dispatchDragEvent", {"type": "dragOver", "x": end_x, "y": end_y, "data": data}
        )
        if step_delay_ms:
            await asyncio.sleep(step_delay_ms / 1000)
    await session.call(
        "Input.dispatchDragEvent", {"type": "drop", "x": end_x, "y": end_y, "data": data}
    )
    return {
        "channel": "html5",
        "mime_types": sorted(
            {
                str(item.get("mimeType"))
                for item in (data.get("items") or [])
                if isinstance(item, dict) and item.get("mimeType")
            }
        ),
    }


async def _finish_mouse(
    session: DragSession,
    from_x: float,
    from_y: float,
    end_x: float,
    end_y: float,
    steps: int,
    step_delay_ms: int,
) -> dict[str, Any]:
    """鼠标拖放：分步移动，一步到位很多实现不认。"""

    for index in range(1, steps + 1):
        ratio = index / steps
        await _mouse(
            session,
            "mouseMoved",
            from_x + (end_x - from_x) * ratio,
            from_y + (end_y - from_y) * ratio,
            buttons=1,
        )
        if step_delay_ms:
            await asyncio.sleep(step_delay_ms / 1000)
    # 有的实现只在最后一次 mousemove 之后才更新放置目标，释放前再补一次。
    await _mouse(session, "mouseMoved", end_x, end_y, buttons=1)
    await _mouse(session, "mouseReleased", end_x, end_y, buttons=0, click_count=1)
    return {"channel": "pointer", "steps": steps}


async def _mouse(
    session: DragSession,
    kind: str,
    x: float,
    y: float,
    *,
    buttons: int,
    click_count: int | None = None,
) -> None:
    params: dict[str, Any] = {"type": kind, "x": x, "y": y, "buttons": buttons}
    if kind != "mouseMoved" or buttons:
        params["button"] = "left"
    if click_count is not None:
        params["clickCount"] = click_count
    await session.call("Input.dispatchMouseEvent", params)


def _kickoff_point(
    start_x: float, start_y: float, end_x: float, end_y: float
) -> tuple[float, float]:
    delta_x, delta_y = end_x - start_x, end_y - start_y
    distance = (delta_x**2 + delta_y**2) ** 0.5
    if distance < 1e-6:
        return start_x + _DRAG_KICKOFF_PX, start_y
    ratio = min(1.0, _DRAG_KICKOFF_PX / distance)
    return start_x + delta_x * ratio, start_y + delta_y * ratio

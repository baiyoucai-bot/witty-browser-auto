"""通过 CDP 发送完整、可恢复的鼠标指针序列。"""

from __future__ import annotations

import asyncio
import logging

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.domain.errors import (
    ActionOutcomeUnknownError,
    CdpCommandError,
    CdpDisconnectedError,
)

logger = logging.getLogger(__name__)

_VISUAL_APPROACH = ((48, 8, 18), (32, 5, 20), (20, 3, 22), (10, 1, 24), (4, 0, 26), (0, 0, 60))
VISUAL_DRAG_APPROACH_POINTS = len(_VISUAL_APPROACH)
VISUAL_DRAG_APPROACH_DURATION_MS = sum(point[2] for point in _VISUAL_APPROACH)
VISUAL_DRAG_RELEASE_SETTLE_SECONDS = 0.08
POINTER_RELEASE_RECOVERY_TIMEOUT_SECONDS = 0.25
_POINTER_FEEDBACK_RADIUS = 7
_POINTER_FEEDBACK_TIMEOUT_SECONDS = 0.5
_POINTER_BUTTON_MASKS = {"left": 1, "right": 2, "middle": 4}
POINTER_BUTTONS = tuple(_POINTER_BUTTON_MASKS)
_MAX_CLICK_COUNT = 3
MAX_CLICK_COUNT = _MAX_CLICK_COUNT


async def _enable_pointer_feedback(session: CdpTargetSession) -> bool:
    """启用浏览器原生 Overlay；失败只影响可见反馈，不影响真实输入。"""

    try:
        await session.call(
            "Overlay.enable",
            timeout_seconds=_POINTER_FEEDBACK_TIMEOUT_SECONDS,
        )
    except (CdpCommandError, CdpDisconnectedError, TimeoutError):
        logger.info("浏览器不支持可视指针反馈，继续派发真实鼠标轨迹", exc_info=True)
        return False
    return True


async def _queue_pointer_feedback(
    session: CdpTargetSession,
    pending: list[asyncio.Task[dict[str, object]]],
    x: float,
    y: float,
) -> None:
    """用 DevTools Overlay 同步显示轨迹，不向页面 DOM 注入节点或事件。"""

    radius = _POINTER_FEEDBACK_RADIUS
    task = asyncio.create_task(
        session.call(
            "Overlay.highlightQuad",
            {
                "quad": [
                    x - radius,
                    y - radius,
                    x + radius,
                    y - radius,
                    x + radius,
                    y + radius,
                    x - radius,
                    y + radius,
                ],
                "color": {"r": 255, "g": 255, "b": 255, "a": 0.72},
                "outlineColor": {"r": 31, "g": 111, "b": 87, "a": 0.96},
            },
            timeout_seconds=_POINTER_FEEDBACK_TIMEOUT_SECONDS,
        )
    )
    pending.append(task)
    await asyncio.sleep(0)


async def _disable_pointer_feedback(
    session: CdpTargetSession,
    pending: list[asyncio.Task[dict[str, object]]],
) -> None:
    results = await asyncio.gather(*pending, return_exceptions=True)
    if any(isinstance(result, Exception) for result in results):
        logger.info("部分可视指针帧未显示，真实鼠标轨迹已经继续执行")
    for method in ("Overlay.hideHighlight", "Overlay.disable"):
        try:
            await session.call(method, timeout_seconds=_POINTER_FEEDBACK_TIMEOUT_SECONDS)
        except (CdpCommandError, CdpDisconnectedError, TimeoutError):
            logger.info("清理可视指针反馈失败，浏览器会在页面切换时自动清理", exc_info=True)
            break


async def _queue_pointer_event(
    session: CdpTargetSession,
    pending: list[asyncio.Task[dict[str, object]]],
    params: dict[str, object],
    *,
    yield_after_queue: bool = True,
) -> None:
    """先发送输入事件，延迟到序列末尾再等待 Chrome 的确认响应。"""

    task = asyncio.create_task(session.call("Input.dispatchMouseEvent", params))
    pending.append(task)
    # Chrome 会把孤立的 mouseMoved 确认延迟数秒；让任务先完成 WebSocket 写入，
    # 后续事件即可按顺序进入同一个 CDP 管道并触发浏览器立即确认整组输入。
    if yield_after_queue:
        await asyncio.sleep(0)
    if task.done():
        task.result()


async def _cancel_pending(pending: list[asyncio.Task[dict[str, object]]]) -> None:
    for task in pending:
        if not task.done():
            task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def _recover_pointer_release(
    session: CdpTargetSession,
    x: float,
    y: float,
    *,
    button: str = "left",
) -> None:
    release = asyncio.create_task(
        session.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": x,
                "y": y,
                "button": button,
                "buttons": 0,
                "clickCount": 1,
            },
        )
    )
    try:
        await asyncio.sleep(0)
        async with asyncio.timeout(POINTER_RELEASE_RECOVERY_TIMEOUT_SECONDS):
            await asyncio.shield(release)
    except (Exception, asyncio.CancelledError):
        if not release.done():
            release.cancel()
        await asyncio.gather(release, return_exceptions=True)
        logger.warning("拖拽中断后未收到释放确认，释放命令已经送入 CDP 管道")


async def _approach_pointer(
    session: CdpTargetSession,
    start_x: float,
    start_y: float,
    pending: list[asyncio.Task[dict[str, object]]],
    feedback_pending: list[asyncio.Task[dict[str, object]]] | None = None,
) -> None:
    """从滑块左下方逐步接近并短暂停留，避免瞬移后立即按下。"""
    for offset_x, offset_y, delay_ms in _VISUAL_APPROACH:
        await _queue_pointer_event(
            session,
            pending,
            {
                "type": "mouseMoved",
                "x": max(0, start_x - offset_x),
                "y": max(0, start_y + offset_y),
                "buttons": 0,
            },
        )
        if feedback_pending is not None:
            await _queue_pointer_feedback(
                session,
                feedback_pending,
                max(0, start_x - offset_x),
                max(0, start_y + offset_y),
            )
        await asyncio.sleep(delay_ms / 1000)


def resolve_pointer(button: object = "left", click_count: object = 1) -> tuple[str, int]:
    """校验工具参数里的鼠标按键与连击次数；两者省略时保持普通左键单击。"""

    if button is None:
        button = "left"
    if button not in _POINTER_BUTTON_MASKS:
        raise ValueError(f"button 只能是 {'、'.join(POINTER_BUTTONS)}")
    if click_count is None:
        click_count = 1
    if (
        isinstance(click_count, bool)
        or not isinstance(click_count, int)
        or not 1 <= click_count <= _MAX_CLICK_COUNT
    ):
        raise ValueError(f"click_count 必须是 1 到 {_MAX_CLICK_COUNT} 的整数")
    return str(button), click_count


async def dispatch_hover(session: CdpTargetSession, x: float, y: float) -> None:
    """只移动指针，不按下任何按键。

    实测单条 `mouseMoved` 即可触发 `mouseover`/`mouseenter`/`mousemove` 并让 CSS `:hover` 生效。
    """

    pending: list[asyncio.Task[dict[str, object]]] = []
    try:
        await _queue_pointer_event(
            session,
            pending,
            {"type": "mouseMoved", "x": x, "y": y, "buttons": 0},
            yield_after_queue=False,
        )
        await asyncio.gather(*pending)
    except (Exception, asyncio.CancelledError):
        await _cancel_pending(pending)
        raise


async def dispatch_click(
    session: CdpTargetSession,
    x: float,
    y: float,
    *,
    button: str = "left",
    click_count: int = 1,
) -> None:
    """流水线发送移动、按下和释放，避免逐条等待浏览器输入确认。

    双击必须发两轮递增 `clickCount` 的按下/释放：只发一轮 `clickCount=2` 虽然也能触发
    `dblclick`，但页面收不到第一次 `click`，与真实用户双击的事件序列不一致。
    右键无需额外派发 `contextmenu`，Chrome 会在 `mousedown` 与 `mouseup` 之间自动补上。
    """

    if button not in _POINTER_BUTTON_MASKS:
        raise ValueError(f"不支持的鼠标按键：{button}")
    if not 1 <= click_count <= _MAX_CLICK_COUNT:
        raise ValueError(f"点击次数必须在 1 到 {_MAX_CLICK_COUNT} 之间")
    mask = _POINTER_BUTTON_MASKS[button]
    pending: list[asyncio.Task[dict[str, object]]] = []
    press_dispatched = False
    try:
        await _queue_pointer_event(
            session,
            pending,
            {"type": "mouseMoved", "x": x, "y": y, "buttons": 0},
        )
        for count in range(1, click_count + 1):
            last = count == click_count
            await _queue_pointer_event(
                session,
                pending,
                {
                    "type": "mousePressed",
                    "x": x,
                    "y": y,
                    "button": button,
                    "buttons": mask,
                    "clickCount": count,
                },
            )
            press_dispatched = True
            await _queue_pointer_event(
                session,
                pending,
                {
                    "type": "mouseReleased",
                    "x": x,
                    "y": y,
                    "button": button,
                    "buttons": 0,
                    "clickCount": count,
                },
                yield_after_queue=not last,
            )
        await asyncio.gather(*pending)
    except (Exception, asyncio.CancelledError) as exc:
        await _cancel_pending(pending)
        if press_dispatched:
            await _recover_pointer_release(session, x, y, button=button)
            raise ActionOutcomeUnknownError("点击过程被中断，页面状态可能已经改变") from exc
        raise


async def dispatch_drag(
    session: CdpTargetSession,
    points: tuple[tuple[float, float, int], ...],
    *,
    approach: bool = False,
) -> bool:
    """发送移动、按下、拖动和释放；按下后的异常按结果未知处理。"""
    start_x, start_y, _ = points[0]
    current_x, current_y = start_x, start_y
    press_dispatched = False
    released = False
    pending: list[asyncio.Task[dict[str, object]]] = []
    feedback_pending: list[asyncio.Task[dict[str, object]]] = []
    feedback_enabled = approach and await _enable_pointer_feedback(session)
    try:
        if approach:
            await _approach_pointer(
                session,
                start_x,
                start_y,
                pending,
                feedback_pending if feedback_enabled else None,
            )
        else:
            await _queue_pointer_event(
                session,
                pending,
                {"type": "mouseMoved", "x": start_x, "y": start_y, "buttons": 0},
            )
        # 发送按下命令后即视为可能已产生页面副作用，后续失败不能判定为安全失败。
        await _queue_pointer_event(
            session,
            pending,
            {
                "type": "mousePressed",
                "x": start_x,
                "y": start_y,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            },
        )
        press_dispatched = True
        if points[0][2]:
            await asyncio.sleep(points[0][2] / 1000)
        for current_x, current_y, delay_ms in points[1:]:
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
            await _queue_pointer_event(
                session,
                pending,
                {
                    "type": "mouseMoved",
                    "x": current_x,
                    "y": current_y,
                    "button": "left",
                    "buttons": 1,
                },
            )
            if feedback_enabled:
                await _queue_pointer_feedback(
                    session,
                    feedback_pending,
                    current_x,
                    current_y,
                )
        if approach:
            await asyncio.sleep(VISUAL_DRAG_RELEASE_SETTLE_SECONDS)
        await _queue_pointer_event(
            session,
            pending,
            {
                "type": "mouseReleased",
                "x": current_x,
                "y": current_y,
                "button": "left",
                "buttons": 0,
                "clickCount": 1,
            },
            yield_after_queue=False,
        )
        await asyncio.gather(*pending)
        released = True
    except (Exception, asyncio.CancelledError) as exc:
        await _cancel_pending(pending)
        if press_dispatched and not released:
            await _recover_pointer_release(session, current_x, current_y)
            raise ActionOutcomeUnknownError("拖拽过程被中断，页面状态可能已经改变") from exc
        raise
    finally:
        if feedback_enabled:
            await _disable_pointer_feedback(session, feedback_pending)
    return feedback_enabled

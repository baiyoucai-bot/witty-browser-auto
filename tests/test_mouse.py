from __future__ import annotations

import asyncio
from typing import Any

from witty_browser_auto.browser.mouse import VISUAL_DRAG_RELEASE_SETTLE_SECONDS, dispatch_drag


class _Session:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if method == "Input.dispatchMouseEvent":
            self.events.append(params or {})
        else:
            assert method.startswith("Overlay.")
        return {}


def test_visual_drag_settles_at_endpoint_before_release(monkeypatch: Any) -> None:
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("witty_browser_auto.browser.mouse.asyncio.sleep", record_sleep)
    session = _Session()

    asyncio.run(dispatch_drag(session, ((40, 20, 0), (100, 20, 30)), approach=True))

    assert sleeps[-1] == VISUAL_DRAG_RELEASE_SETTLE_SECONDS
    assert [event["type"] for event in session.events[-3:]] == [
        "mousePressed",
        "mouseMoved",
        "mouseReleased",
    ]

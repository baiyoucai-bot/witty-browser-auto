from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from witty_browser_auto.browser.driver import (
    CdpAutomationDriver,
    _observation_fingerprint,
    _pointer_candidate_name,
    _sanitize_visual_resources,
)
from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.config import BrowserConfig
from witty_browser_auto.domain.errors import CdpCommandError, TargetNotFoundError
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionKind,
    BoundingBox,
    CandidateTarget,
    DragRiskClass,
    ExpectedCondition,
    LocatorRecipe,
    Observation,
)


class FailingDriver(CdpAutomationDriver):
    def __init__(self, artifact_root: Path, error: CdpCommandError) -> None:
        super().__init__(BrowserConfig(), artifact_root)
        self.error = error

    async def _execute_locked(self, command: ActionCommand) -> dict[str, Any]:
        raise self.error


def test_pointer_icon_uses_stable_attribute_as_semantic_name() -> None:
    assert (
        _pointer_candidate_name("\ue685", "\ue685", {"id": "captcha-sliding-refresh"})
        == "captcha-sliding-refresh"
    )
    assert _pointer_candidate_name("刷新验证码", "刷新验证码", {}) == "刷新验证码"


def test_rebound_driver_preserves_current_page_for_next_open(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = CdpAutomationDriver(BrowserConfig(), tmp_path / "first")
        driver.session = cast(
            CdpTargetSession,
            SimpleNamespace(target_id="current-page", session_id="current-session"),
        )
        navigate = AsyncMock()
        driver._navigate = navigate

        driver.rebind_task_context(tmp_path / "continuation", None, None)

        assert await driver.open("https://example.com/start") == "current-page"
        navigate.assert_not_awaited()
        assert driver.artifact_root == tmp_path / "continuation"

        await driver.open("https://example.com/next")
        navigate.assert_awaited_once_with("https://example.com/next", timeout_seconds=30)

    asyncio.run(scenario())


def test_reattached_driver_preserves_current_page_on_first_open(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = CdpAutomationDriver(BrowserConfig(), tmp_path)
        navigate = AsyncMock()
        driver._navigate = navigate

        async def reattach() -> None:
            driver.session = cast(
                CdpTargetSession,
                SimpleNamespace(target_id="reattached-page", session_id="reattached-session"),
            )
            driver._preserve_page_on_next_open = True

        driver.start = AsyncMock(side_effect=reattach)

        assert await driver.open("https://example.com/start") == "reattached-page"
        driver.start.assert_awaited_once_with()
        navigate.assert_not_awaited()

    asyncio.run(scenario())


def test_driver_recovers_closed_page_in_existing_browser_connection(tmp_path: Path) -> None:
    class StubBrowser:
        def __init__(self, recovered_session: object) -> None:
            self.recovered_session = recovered_session
            self.created_with: tuple[str | None, str] | None = None
            self.remembered_target = ""

        @staticmethod
        def is_session_active(session: object) -> bool:
            return getattr(session, "target_id", "") == "recovered-page"

        async def create_page(self, context_id: str | None, url: str) -> object:
            self.created_with = (context_id, url)
            return self.recovered_session

        def remember_target(self, target_id: str) -> None:
            self.remembered_target = target_id

    async def scenario() -> None:
        stale = SimpleNamespace(target_id="closed-page", session_id="closed-session")
        recovered = SimpleNamespace(target_id="recovered-page", session_id="recovered-session")
        driver = CdpAutomationDriver(BrowserConfig(), tmp_path)
        driver.session = cast(CdpTargetSession, stale)
        driver.context_id = "task-context"
        driver._last_known_url = "https://example.com/orders?page=2"
        browser = StubBrowser(recovered)
        driver.browser = cast(Any, browser)

        async def adopt(session: object) -> None:
            driver.session = cast(CdpTargetSession, session)

        driver._adopt_page_session = AsyncMock(side_effect=adopt)

        assert await driver._ensure_active_page() is True
        assert driver.session is recovered
        assert browser.created_with == ("task-context", "https://example.com/orders?page=2")
        assert browser.remembered_target == "recovered-page"
        assert driver._page_recovered_since_observation is True
        assert await driver._ensure_active_page() is False
        assert driver._adopt_page_session.await_count == 1

    asyncio.run(scenario())


def test_request_page_attention_restores_minimized_window_and_activates_tab(
    tmp_path: Path,
) -> None:
    class StubConnection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.calls.append((method, params or {}))
            if method == "Browser.getWindowForTarget":
                return {"windowId": 7, "bounds": {"windowState": "minimized"}}
            return {}

    class StubSession:
        target_id = "order-page"

        def __init__(self, connection: StubConnection) -> None:
            self.connection = connection
            self.calls: list[str] = []

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(method)
            return {}

    async def scenario() -> None:
        connection = StubConnection()
        session = StubSession(connection)
        driver = CdpAutomationDriver(BrowserConfig(), tmp_path)
        driver.session = session  # type: ignore[assignment]

        await driver.request_page_attention()

        assert connection.calls == [
            ("Browser.getWindowForTarget", {"targetId": "order-page"}),
            (
                "Browser.setWindowBounds",
                {"windowId": 7, "bounds": {"windowState": "normal"}},
            ),
        ]
        assert session.calls == ["Page.bringToFront"]

    asyncio.run(scenario())


def test_visual_resource_sanitization_keeps_only_hashes_and_dimensions() -> None:
    resources = _sanitize_visual_resources(
        [
            ["source-hash", "pixel-hash", 120, 40],
            ["https://secret.example/image.png", "", True, 40],
            ["incomplete"],
        ]
    )

    assert resources == (("source-hash", "pixel-hash", 120, 40),)
    assert "secret.example" not in str(resources)


def test_observation_fingerprint_changes_when_visible_image_pixels_change() -> None:
    first = _observation_fingerprint(
        "https://example.com/order",
        "订单查询",
        [],
        DragRiskClass.SECURITY,
        (("same-source", "first-pixels", 120, 40),),
    )
    refreshed = _observation_fingerprint(
        "https://example.com/order",
        "订单查询",
        [],
        DragRiskClass.SECURITY,
        (("same-source", "second-pixels", 120, 40),),
    )

    assert refreshed != first


def test_observation_fingerprint_changes_when_only_visible_text_changes() -> None:
    """展开、加购、状态切换这类点击只改文字不改候选，指纹必须能看见。"""

    before = _observation_fingerprint(
        "https://example.com/cart",
        "购物车",
        [],
        DragRiskClass.UNKNOWN,
        (),
        text="购物车 (1)\n合计 ¥99",
    )
    after = _observation_fingerprint(
        "https://example.com/cart",
        "购物车",
        [],
        DragRiskClass.UNKNOWN,
        (),
        text="购物车 (2)\n合计 ¥198",
    )
    whitespace_only = _observation_fingerprint(
        "https://example.com/cart",
        "购物车",
        [],
        DragRiskClass.UNKNOWN,
        (),
        text="购物车  (1)\n\n合计 ¥99  ",
    )

    assert after != before
    assert whitespace_only == before


def test_cdp_timeout_marks_action_outcome_unknown(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = FailingDriver(
            tmp_path,
            CdpCommandError("CDP 命令执行超时", method="Input.dispatchMouseEvent"),
        )

        receipt = await driver.execute(ActionCommand("click", ActionKind.CLICK, target_id="target"))

        assert receipt.success is False
        assert receipt.outcome_known is False

    asyncio.run(scenario())


def test_entire_browser_action_shares_one_timeout_budget(tmp_path: Path) -> None:
    class StalledDriver(CdpAutomationDriver):
        async def _execute_locked(self, command: ActionCommand) -> dict[str, Any]:
            await asyncio.Event().wait()
            return {}

    async def scenario() -> None:
        driver = StalledDriver(BrowserConfig(), tmp_path)

        receipt = await driver.execute(
            ActionCommand(
                "stalled-click",
                ActionKind.CLICK,
                target_id="target",
                timeout_seconds=0.01,
            )
        )

        assert receipt.success is False
        assert receipt.outcome_known is False
        assert "总时间预算" in receipt.message
        assert receipt.duration_ms < 200

    asyncio.run(scenario())


def test_cdp_protocol_rejection_marks_action_outcome_known(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = FailingDriver(
            tmp_path,
            CdpCommandError(
                "节点不存在",
                method="DOM.focus",
                error_code=-32000,
            ),
        )

        receipt = await driver.execute(
            ActionCommand("input", ActionKind.INPUT_TEXT, target_id="target", value="value")
        )

        assert receipt.success is False
        assert receipt.outcome_known is True

    asyncio.run(scenario())


def test_input_text_is_verified_by_dom_readback(tmp_path: Path) -> None:
    class StubSession:
        target_id = "page"
        observation_version = 1

        def __init__(self) -> None:
            self.inserted_text = ""
            self.function_calls = 0

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            if method == "Input.insertText":
                self.inserted_text = str((params or {}).get("text", ""))
                return {}
            if method == "Runtime.callFunctionOn":
                self.function_calls += 1
                if self.function_calls == 2:
                    expected = (params or {}).get("arguments", [{}])[0].get("value")
                    return {"result": {"value": expected == self.inserted_text}}
            return {}

    class InputDriver(CdpAutomationDriver):
        async def _resolve_target(
            self,
            target_id: str,
        ) -> tuple[CandidateTarget, BoundingBox, str]:
            return candidate, BoundingBox(0, 0, 100, 30), "input-object"

    async def scenario() -> None:
        session = StubSession()
        driver = InputDriver(BrowserConfig(), tmp_path)
        driver.session = session  # type: ignore[assignment]
        receipt = await driver.execute(
            ActionCommand(
                "input",
                ActionKind.INPUT_TEXT,
                target_id=candidate.target_id,
                value="private-value",
                idempotent=True,
            )
        )

        assert receipt.success is True
        assert receipt.data["输入回读一致"] is True
        assert receipt.data["输入长度"] == len("private-value")
        assert "private-value" not in str(receipt.data)

    candidate = CandidateTarget(
        target_id="page:1:10",
        role="textbox",
        name="订单账号",
        text="",
        confidence=0.95,
        reasons=("测试",),
        recipe=LocatorRecipe("ax_backend_node", backend_node_id=10),
        box=BoundingBox(0, 0, 100, 30),
    )
    asyncio.run(scenario())


def test_target_from_old_observation_is_rejected_before_cdp_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = CdpAutomationDriver(BrowserConfig(), tmp_path)
        driver.session = SimpleNamespace(target_id="page", observation_version=2)
        candidate = CandidateTarget(
            target_id="page:1:10",
            role="button",
            name="提交",
            text="提交",
            confidence=0.95,
            reasons=("测试",),
            recipe=LocatorRecipe("ax_backend_node", backend_node_id=10),
            box=BoundingBox(0, 0, 100, 30),
        )
        driver._candidate_cache[candidate.target_id] = candidate

        with pytest.raises(TargetNotFoundError, match="页面状态已变化"):
            await driver._resolve_target(candidate.target_id)

    asyncio.run(scenario())


def test_driver_declares_fingerprint_bound_visual_actions(tmp_path: Path) -> None:
    driver = CdpAutomationDriver(BrowserConfig(), tmp_path)

    assert driver.capabilities.visual is True


def test_click_adopts_page_opened_by_target_blank_link(tmp_path: Path) -> None:
    class StubConnection:
        def __init__(self) -> None:
            self.page_future: asyncio.Future[CdpEvent] | None = None
            self.page_predicate: Any = None

        def subscribe(
            self,
            method: str,
            handler: Any,
            *,
            session_id: str | None = None,
        ) -> Any:
            return lambda: None

        @asynccontextmanager
        async def expect_event(
            self,
            method: str,
            *,
            session_id: str | None = None,
            predicate: Any = None,
        ) -> Any:
            assert method == "Target.attachedToTarget"
            self.page_future = asyncio.get_running_loop().create_future()
            self.page_predicate = predicate
            yield self.page_future

    class StubSession:
        def __init__(self, target_id: str, session_id: str, connection: StubConnection) -> None:
            self.target_id = target_id
            self.session_id = session_id
            self.connection = connection
            self.observation_version = 1
            self.calls: list[str] = []

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            self.calls.append(method)
            if (
                method == "Input.dispatchMouseEvent"
                and (params or {}).get("type") == "mouseReleased"
            ):
                event = CdpEvent(
                    "Target.attachedToTarget",
                    {
                        "sessionId": "session-new",
                        "targetInfo": {
                            "targetId": "page-new",
                            "type": "page",
                            "openerId": "page-old",
                        },
                    },
                )
                assert self.connection.page_future is not None
                assert self.connection.page_predicate(event) is True
                self.connection.page_future.set_result(event)
            return {}

    class StubBrowser:
        def __init__(self, session: StubSession) -> None:
            self.session = session
            self.requested_target_id = ""

        async def wait_for_target_session(
            self,
            target_id: str,
            *,
            timeout_seconds: float,
        ) -> StubSession:
            self.requested_target_id = target_id
            return self.session

    class NewPageDriver(CdpAutomationDriver):
        async def _resolve_target(
            self,
            target_id: str,
        ) -> tuple[CandidateTarget, BoundingBox, str]:
            return candidate, BoundingBox(10, 10, 100, 30), "object-link"

    async def scenario() -> None:
        connection = StubConnection()
        old_session = StubSession("page-old", "session-old", connection)
        new_session = StubSession("page-new", "session-new", connection)
        driver = NewPageDriver(BrowserConfig(), tmp_path)
        driver.session = old_session  # type: ignore[assignment]
        browser = StubBrowser(new_session)
        driver.browser = browser  # type: ignore[assignment]
        driver._owned_target_ids.add("page-old")

        receipt = await driver.execute(
            ActionCommand("open-merchant", ActionKind.CLICK, target_id=candidate.target_id)
        )

        assert receipt.success is True
        assert receipt.data["new_page"] is True
        assert receipt.data["target_id"] == "page-new"
        assert browser.requested_target_id == "page-new"
        assert driver.session is new_session
        assert "Page.bringToFront" in new_session.calls
        assert driver._owned_target_ids == {"page-old", "page-new"}

    candidate = CandidateTarget(
        target_id="page-old:1:10",
        role="link",
        name="商家中心",
        text="商家中心",
        confidence=0.95,
        reasons=("测试",),
        recipe=LocatorRecipe(
            "ax_backend_node",
            role="link",
            name="商家中心",
            value=(
                '{"attrs":{"href":"/merchant","target":"_blank"},'
                '"name":"商家中心","role":"link","tag":"a","text":"商家中心"}'
            ),
            backend_node_id=10,
        ),
        box=BoundingBox(10, 10, 100, 30),
    )
    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("reuse_profile", "expected_context"),
    [(True, None), (False, "isolated-context")],
)
def test_driver_reuses_default_context_only_for_persistent_profile(
    tmp_path: Path,
    reuse_profile: bool,
    expected_context: str | None,
) -> None:
    class StubConnection:
        def subscribe(
            self,
            method: str,
            handler: Any,
            *,
            session_id: str | None = None,
        ) -> Any:
            return lambda: None

    class StubSession:
        target_id = "page"
        session_id = "session"
        observation_version = 1
        connection = StubConnection()

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            if method in {"Log.enable", "Target.setAutoAttach"}:
                return {}
            assert method == "Runtime.evaluate"
            return {
                "result": {
                    "value": {
                        "webdriver": False,
                        "userAgent": "Chrome",
                        "language": "zh-CN",
                        "platform": "MacIntel",
                        "timezone": "Asia/Shanghai",
                        "visibilityState": "visible",
                    }
                }
            }

    class StubBrowser:
        def __init__(self) -> None:
            self.config = BrowserConfig(reuse_profile=reuse_profile)
            self.reattached = False
            self.created_contexts = 0
            self.page_context: str | None = "not-called"
            self.remembered_target: str | None = None

        async def start(self) -> None:
            return None

        async def create_context(self) -> str:
            self.created_contexts += 1
            return "isolated-context"

        async def create_page(self, context_id: str | None) -> StubSession:
            self.page_context = context_id
            return StubSession()

        def remember_target(self, target_id: str) -> None:
            self.remembered_target = target_id

    async def scenario() -> None:
        driver = CdpAutomationDriver(BrowserConfig(reuse_profile=reuse_profile), tmp_path)
        browser = StubBrowser()
        driver.browser = browser  # type: ignore[assignment]

        await driver.start()

        assert browser.page_context == expected_context
        assert browser.created_contexts == (0 if reuse_profile else 1)
        assert browser.remembered_target == "page"
        assert driver.environment_diagnostics["webdriver"] is False
        assert driver.environment_diagnostics["timezone"] == "Asia/Shanghai"
        assert driver.network_recorder is not None
        assert driver.page_diagnostics is not None
        await driver.network_recorder.close()
        await driver.page_diagnostics.close()

    asyncio.run(scenario())


def test_takeover_mode_borrows_existing_page_and_preserves_it_on_close(tmp_path: Path) -> None:
    class StubConnection:
        def subscribe(
            self,
            method: str,
            handler: Any,
            *,
            session_id: str | None = None,
        ) -> Any:
            return lambda: None

    class StubSession:
        target_id = "borrowed-page"
        session_id = "borrowed-session"
        observation_version = 1
        connection = StubConnection()

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "value": {
                            "webdriver": False,
                            "userAgent": "Chrome",
                            "language": "zh-CN",
                            "platform": "MacIntel",
                            "timezone": "Asia/Shanghai",
                            "visibilityState": "visible",
                        }
                    }
                }
            return {}

    class StubBrowser:
        def __init__(self) -> None:
            self.config = BrowserConfig(session_mode="takeover")  # type: ignore[arg-type]
            self.reattached = False
            self.claimed = 0
            self.created_pages = 0
            self.closed_targets: list[str] = []
            self.closed = False

        async def start(self) -> None:
            return None

        async def claim_existing_page(self) -> StubSession:
            self.claimed += 1
            return StubSession()

        async def create_context(self) -> str:
            raise AssertionError("接管现有页面时不应创建隔离上下文")

        async def create_page(self, context_id: str | None) -> StubSession:
            self.created_pages += 1
            return StubSession()

        def remember_target(self, target_id: str) -> None:
            return None

        async def close_target(self, target_id: str) -> None:
            self.closed_targets.append(target_id)

        async def dispose_context(self, context_id: str) -> None:
            raise AssertionError("接管现有页面时没有 BrowserContext 需要销毁")

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        driver = CdpAutomationDriver(BrowserConfig(session_mode="takeover"), tmp_path)  # type: ignore[arg-type]
        browser = StubBrowser()
        driver.browser = browser  # type: ignore[assignment]

        await driver.start()
        assert browser.claimed == 1
        assert browser.created_pages == 0
        assert driver._owned_target_ids == set()
        assert driver._borrowed_target_ids == {"borrowed-page"}

        await driver.close()

        assert browser.closed_targets == []
        assert browser.closed is True

    asyncio.run(scenario())


def test_takeover_open_reuses_matching_current_page(tmp_path: Path) -> None:
    async def scenario() -> None:
        current_session = SimpleNamespace(
            target_id="borrowed-page",
            session_id="borrowed-session",
            call=AsyncMock(
                return_value={
                    "result": {
                        "value": "https://example.com/orders?status=pending#results",
                    }
                }
            ),
        )
        browser = SimpleNamespace(
            create_window=AsyncMock(),
            remember_target=lambda target_id: None,
        )
        driver = CdpAutomationDriver(BrowserConfig(session_mode="takeover"), tmp_path)  # type: ignore[arg-type]
        driver.browser = browser  # type: ignore[assignment]
        driver.session = cast(CdpTargetSession, current_session)
        driver._borrowed_target_ids.add("borrowed-page")
        driver._preserve_page_on_next_open = True
        driver._takeover_page_selection_pending = True
        driver._ensure_active_page = AsyncMock(return_value=False)  # type: ignore[method-assign]
        driver._navigate = AsyncMock()  # type: ignore[method-assign]

        target_id = await driver.open("https://example.com/orders?from=task")

        assert target_id == "borrowed-page"
        browser.create_window.assert_not_awaited()
        driver._navigate.assert_not_awaited()

    asyncio.run(scenario())


def test_takeover_open_creates_same_browser_window_when_current_page_mismatches(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        current_session = SimpleNamespace(
            target_id="borrowed-page",
            session_id="borrowed-session",
            call=AsyncMock(return_value={"result": {"value": "https://example.com/profile"}}),
        )
        new_session = SimpleNamespace(target_id="task-window", session_id="task-session")
        browser = SimpleNamespace(
            create_window=AsyncMock(return_value=new_session),
            close_target=AsyncMock(),
            remember_target=lambda target_id: None,
        )
        driver = CdpAutomationDriver(BrowserConfig(session_mode="takeover"), tmp_path)  # type: ignore[arg-type]
        driver.browser = browser  # type: ignore[assignment]
        driver.session = cast(CdpTargetSession, current_session)
        driver._borrowed_target_ids.add("borrowed-page")
        driver._preserve_page_on_next_open = True
        driver._takeover_page_selection_pending = True
        driver._ensure_active_page = AsyncMock(return_value=False)  # type: ignore[method-assign]

        async def adopt(session: object, *, borrowed: bool = False) -> None:
            assert borrowed is False
            driver.session = cast(CdpTargetSession, session)
            driver._owned_target_ids.add("task-window")

        driver._adopt_page_session = AsyncMock(side_effect=adopt)  # type: ignore[method-assign]

        target_id = await driver.open("https://example.com/orders")

        assert target_id == "task-window"
        browser.create_window.assert_awaited_once_with(None, "https://example.com/orders")
        browser.close_target.assert_not_awaited()
        assert driver._borrowed_target_ids == {"borrowed-page"}
        assert driver._owned_target_ids == {"task-window"}

    asyncio.run(scenario())


def test_takeover_mode_creates_window_when_no_page_can_be_borrowed(tmp_path: Path) -> None:
    class StubConnection:
        def subscribe(
            self,
            method: str,
            handler: Any,
            *,
            session_id: str | None = None,
        ) -> Any:
            return lambda: None

    class StubSession:
        target_id = "task-window"
        session_id = "task-session"
        observation_version = 1
        connection = StubConnection()

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "value": {
                            "webdriver": False,
                            "userAgent": "Chrome",
                            "language": "zh-CN",
                            "platform": "MacIntel",
                            "timezone": "Asia/Shanghai",
                            "visibilityState": "visible",
                        }
                    }
                }
            return {}

    class StubBrowser:
        def __init__(self) -> None:
            self.config = BrowserConfig(session_mode="takeover")  # type: ignore[arg-type]
            self.reattached = True
            self.created_windows = 0
            self.remembered_target = ""

        async def start(self) -> None:
            return None

        async def claim_existing_page(self) -> None:
            return None

        async def create_window(self, context_id: str | None) -> StubSession:
            assert context_id is None
            self.created_windows += 1
            return StubSession()

        def remember_target(self, target_id: str) -> None:
            self.remembered_target = target_id

    async def scenario() -> None:
        driver = CdpAutomationDriver(BrowserConfig(session_mode="takeover"), tmp_path)  # type: ignore[arg-type]
        browser = StubBrowser()
        driver.browser = browser  # type: ignore[assignment]

        await driver.start()

        assert browser.created_windows == 1
        assert browser.remembered_target == "task-window"
        assert driver._borrowed_target_ids == set()
        assert driver._owned_target_ids == {"task-window"}
        assert driver.network_recorder is not None
        assert driver.page_diagnostics is not None
        await driver.network_recorder.close()
        await driver.page_diagnostics.close()

    asyncio.run(scenario())


def test_drag_risk_classification_is_fail_closed() -> None:
    page_risk, page_reasons = CdpAutomationDriver._classify_page_drag_risk(
        "https://example.com/settings",
        "设置",
        "调整业务进度",
    )
    business_risk, _ = CdpAutomationDriver._classify_candidate_drag_risk(
        role="slider",
        tag="input",
        attributes={"type": "range"},
        page_drag_risk=page_risk,
        page_drag_risk_reasons=page_reasons,
    )
    unknown_risk, _ = CdpAutomationDriver._classify_candidate_drag_risk(
        role="slider",
        tag="div",
        attributes={"role": "slider"},
        page_drag_risk=page_risk,
        page_drag_risk_reasons=page_reasons,
    )
    security_risk, _ = CdpAutomationDriver._classify_page_drag_risk(
        "https://example.com/login",
        "账号登录",
        "请输入手机号和密码",
    )
    informational_risk, _ = CdpAutomationDriver._classify_page_drag_risk(
        "https://example.com/order",
        "订单查询",
        "常见问题：如果短信验证码未收到，请稍后重试。",
    )

    assert page_risk is DragRiskClass.UNKNOWN
    assert business_risk is DragRiskClass.BUSINESS
    assert unknown_risk is DragRiskClass.UNKNOWN
    assert security_risk is DragRiskClass.SECURITY
    assert informational_risk is DragRiskClass.UNKNOWN


def test_observe_fuses_ax_and_dom_candidates_with_backend_dedup(tmp_path: Path) -> None:
    class StubSession:
        def __init__(self) -> None:
            self.target_id = "page"
            self.observation_version = 1

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "value": {
                            "url": "https://example.com",
                            "title": "示例页面",
                            "text": "页面正文",
                        }
                    }
                }
            if method == "Accessibility.getFullAXTree":
                return {
                    "nodes": [
                        {
                            "role": {"value": "button"},
                            "name": {"value": "提交"},
                            "backendDOMNodeId": 10,
                            "properties": [],
                        }
                    ]
                }
            if method == "DOM.getDocument":
                return {
                    "root": {
                        "nodeName": "HTML",
                        "backendNodeId": 1,
                        "children": [
                            {
                                "nodeName": "BODY",
                                "backendNodeId": 2,
                                "children": [
                                    {
                                        "nodeName": "BUTTON",
                                        "backendNodeId": 10,
                                        "attributes": ["id", "submit-btn"],
                                        "children": [{"nodeName": "#text", "nodeValue": "提交"}],
                                    },
                                    {
                                        "nodeName": "INPUT",
                                        "backendNodeId": 20,
                                        "attributes": [
                                            "type",
                                            "text",
                                            "placeholder",
                                            "搜索关键词",
                                            "data-testid",
                                            "search-box",
                                        ],
                                    },
                                    {
                                        "nodeName": "DIV",
                                        "backendNodeId": 30,
                                        "attributes": [
                                            "role",
                                            "button",
                                            "aria-label",
                                            "更多操作",
                                        ],
                                    },
                                    {
                                        "nodeName": "INPUT",
                                        "backendNodeId": 40,
                                        "attributes": ["type", "text", "name", "内部状态"],
                                    },
                                ],
                            }
                        ],
                    }
                }
            if method == "DOM.getBoxModel":
                backend_node_id = (params or {}).get("backendNodeId")
                if backend_node_id == 10:
                    return {"model": {"border": [0, 0, 100, 0, 100, 30, 0, 30]}}
                if backend_node_id == 20:
                    return {"model": {"border": [0, 40, 220, 40, 220, 72, 0, 72]}}
                if backend_node_id == 30:
                    return {"model": {"border": [230, 40, 330, 40, 330, 72, 230, 72]}}
                if backend_node_id == 40:
                    return {}
            raise AssertionError(f"unexpected method: {method}")

    async def scenario() -> None:
        driver = CdpAutomationDriver(BrowserConfig(), tmp_path)
        driver.session = StubSession()  # type: ignore[assignment]

        observation = await driver.observe()

        backend_node_ids = [
            candidate.recipe.backend_node_id for candidate in observation.candidates
        ]
        # 可输入控件排最前(搜索框 20)，随后是按钮：AX 按钮 10 在前、DOM 补充的 30 在后。
        assert backend_node_ids == [20, 10, 30]
        ax_candidate = observation.candidates[1]
        dom_candidate = next(
            candidate
            for candidate in observation.candidates
            if candidate.recipe.backend_node_id == 20
        )
        aria_candidate = next(
            candidate
            for candidate in observation.candidates
            if candidate.recipe.backend_node_id == 30
        )
        assert ax_candidate.recipe.strategy == "ax_backend_node"
        assert dom_candidate.recipe.strategy == "dom_backend_node"
        assert dom_candidate.role == "textbox"
        assert dom_candidate.name == "搜索关键词"
        assert dom_candidate.text == ""
        locator = json.loads(dom_candidate.recipe.value or "{}")
        assert locator["attrs"]["data-testid"] == "search-box"
        assert locator["attrs"]["placeholder"] == "搜索关键词"
        assert aria_candidate.role == "button"
        assert aria_candidate.name == "更多操作"

    asyncio.run(scenario())


def test_observe_and_click_visible_pointer_component(tmp_path: Path) -> None:
    class StubSession:
        target_id = "page"
        observation_version = 1

        def __init__(self) -> None:
            self.mouse_events: list[str] = []

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            payload = params or {}
            if method == "Runtime.evaluate":
                expression = str(payload.get("expression", ""))
                if "MutationObserver" in expression:
                    return {"result": {"value": True}}
                if "__wittyPointerTargets" in expression:
                    return {
                        "result": {
                            "value": [
                                {
                                    "selector": "body > div:nth-of-type(1)",
                                    "tag": "div",
                                    "role": "button",
                                    "name": "订单查询/投诉",
                                    "text": "订单查询/投诉",
                                    "attrs": {},
                                    "disabled": False,
                                    "box": {"x": 300, "y": 10, "width": 120, "height": 32},
                                }
                            ]
                        }
                    }
                if expression.startswith("document.querySelector("):
                    return {"result": {"objectId": "pointer-object"}}
                return {
                    "result": {
                        "value": {
                            "url": "https://example.com",
                            "title": "示例页面",
                            "text": "订单查询/投诉",
                        }
                    }
                }
            if method == "Accessibility.getFullAXTree":
                return {"nodes": []}
            if method == "DOM.getDocument":
                return {"root": {"nodeName": "HTML", "backendNodeId": 1}}
            if method == "Runtime.callFunctionOn":
                declaration = str(payload.get("functionDeclaration", ""))
                if "aria-disabled" in declaration:
                    return {"result": {"value": {"text": "订单查询/投诉", "disabled": False}}}
                return {"result": {"value": True}}
            if method == "DOM.scrollIntoViewIfNeeded":
                assert payload == {"objectId": "pointer-object"}
                return {}
            if method == "DOM.getBoxModel":
                assert payload == {"objectId": "pointer-object"}
                return {"model": {"border": [300, 10, 420, 10, 420, 42, 300, 42]}}
            if method == "Input.dispatchMouseEvent":
                self.mouse_events.append(str(payload.get("type")))
                return {}
            raise AssertionError(f"unexpected method: {method}")

    async def scenario() -> None:
        driver = CdpAutomationDriver(BrowserConfig(), tmp_path)
        session = StubSession()
        driver.session = session  # type: ignore[assignment]

        observation = await driver.observe()

        assert len(observation.candidates) == 1
        candidate = observation.candidates[0]
        assert candidate.name == "订单查询/投诉"
        assert candidate.recipe.strategy == "pointer_css"
        assert candidate.recipe.backend_node_id is None

        receipt = await driver.execute(
            ActionCommand("pointer-click", ActionKind.CLICK, target_id=candidate.target_id)
        )

        assert receipt.success is True
        assert receipt.data["target"] == "订单查询/投诉"
        assert session.mouse_events == ["mouseMoved", "mousePressed", "mouseReleased"]

    asyncio.run(scenario())


def test_observe_keeps_ax_candidates_when_dom_snapshot_is_unavailable(tmp_path: Path) -> None:
    class StubSession:
        target_id = "page"
        observation_version = 1

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "value": {
                            "url": "https://example.com",
                            "title": "示例页面",
                            "text": "页面正文",
                        }
                    }
                }
            if method == "Accessibility.getFullAXTree":
                return {
                    "nodes": [
                        {
                            "role": {"value": "button"},
                            "name": {"value": "提交"},
                            "backendDOMNodeId": 10,
                            "properties": [],
                        }
                    ]
                }
            if method == "DOM.getDocument":
                raise CdpCommandError(
                    "当前浏览器不支持 DOM 快照",
                    method=method,
                    error_code=-32601,
                )
            if method == "DOM.getBoxModel":
                return {"model": {"border": [0, 0, 100, 0, 100, 30, 0, 30]}}
            raise AssertionError(f"unexpected method: {method}")

    async def scenario() -> None:
        driver = CdpAutomationDriver(BrowserConfig(), tmp_path)
        driver.session = StubSession()  # type: ignore[assignment]

        observation = await driver.observe()

        assert len(observation.candidates) == 1
        assert observation.candidates[0].recipe.strategy == "ax_backend_node"
        assert observation.candidates[0].name == "提交"

    asyncio.run(scenario())


def test_observe_waits_for_spa_actionable_dom_before_snapshot(tmp_path: Path) -> None:
    class StubSession:
        target_id = "page"
        observation_version = 1

        def __init__(self) -> None:
            self.hydrated = False

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            if method == "Runtime.evaluate":
                expression = str((params or {}).get("expression", ""))
                if "MutationObserver" in expression:
                    self.hydrated = True
                    return {"result": {"value": True}}
                return {
                    "result": {
                        "value": {
                            "url": "https://example.com",
                            "title": "单页应用",
                            "text": "商家中心",
                        }
                    }
                }
            if method == "Accessibility.getFullAXTree":
                return {"nodes": []}
            if method == "DOM.getDocument":
                children: list[dict[str, Any]] = []
                if self.hydrated:
                    children.append(
                        {
                            "nodeName": "A",
                            "backendNodeId": 10,
                            "attributes": ["href", "/merchant"],
                            "children": [{"nodeName": "#text", "nodeValue": "商家中心"}],
                        }
                    )
                return {
                    "root": {
                        "nodeName": "HTML",
                        "backendNodeId": 1,
                        "children": children,
                    }
                }
            if method == "DOM.getBoxModel":
                return {"model": {"border": [0, 0, 100, 0, 100, 30, 0, 30]}}
            raise AssertionError(f"unexpected method: {method}")

    async def scenario() -> None:
        driver = CdpAutomationDriver(BrowserConfig(), tmp_path)
        driver.session = StubSession()  # type: ignore[assignment]

        observation = await driver.observe()

        assert len(observation.candidates) == 1
        assert observation.candidates[0].role == "link"
        assert observation.candidates[0].name == "商家中心"

    asyncio.run(scenario())


def test_target_exists_relocates_by_stable_semantics(tmp_path: Path) -> None:
    class ObservingDriver(CdpAutomationDriver):
        def __init__(self, observations: list[Observation]) -> None:
            super().__init__(BrowserConfig(), tmp_path)
            self._observations = observations

        async def observe(self, *, force: bool = False) -> Observation:
            return self._observations.pop(0)

    old_candidate = CandidateTarget(
        target_id="page:1:10",
        role="button",
        name="提交",
        text="提交",
        confidence=0.95,
        reasons=("测试",),
        recipe=LocatorRecipe(
            "ax_backend_node",
            role="button",
            name="提交",
            value='{"attrs":{},"name":"提交","role":"button","tag":"","text":"提交"}',
            backend_node_id=10,
        ),
        box=BoundingBox(0, 0, 100, 30),
    )
    relocated = CandidateTarget(
        target_id="page:2:88",
        role="button",
        name="提交",
        text="提交",
        confidence=0.80,
        reasons=("重新观察",),
        recipe=LocatorRecipe(
            "dom_backend_node",
            role="button",
            name="提交",
            value='{"attrs":{"id":"submit-btn"},"name":"提交","role":"button","tag":"button","text":"提交"}',
            backend_node_id=88,
        ),
        box=BoundingBox(0, 0, 100, 30),
    )

    async def scenario() -> None:
        driver = ObservingDriver(
            [
                Observation(
                    surface_id="page",
                    url="https://example.com",
                    title="示例页面",
                    version=2,
                    fingerprint="obs-2",
                    summary="",
                    candidates=(relocated,),
                )
            ]
        )
        driver._candidate_cache[old_candidate.target_id] = old_candidate

        result = await driver.verify(ExpectedCondition("target_exists", old_candidate.target_id))

        assert result.success is True
        assert result.reason == "目标区域存在"

    asyncio.run(scenario())


def test_target_exists_reports_ambiguity_when_multiple_candidates_match(tmp_path: Path) -> None:
    class ObservingDriver(CdpAutomationDriver):
        def __init__(self, observations: list[Observation]) -> None:
            super().__init__(BrowserConfig(), tmp_path)
            self._observations = observations

        async def observe(self, *, force: bool = False) -> Observation:
            return self._observations.pop(0)

    old_candidate = CandidateTarget(
        target_id="page:1:10",
        role="button",
        name="提交",
        text="提交",
        confidence=0.95,
        reasons=("测试",),
        recipe=LocatorRecipe("ax_backend_node", role="button", name="提交", backend_node_id=10),
        box=BoundingBox(0, 0, 100, 30),
    )
    ambiguous = (
        CandidateTarget(
            target_id="page:2:20",
            role="button",
            name="提交",
            text="提交",
            confidence=0.75,
            reasons=("重新观察",),
            recipe=LocatorRecipe(
                "dom_backend_node",
                role="button",
                name="提交",
                backend_node_id=20,
            ),
            box=BoundingBox(0, 0, 100, 30),
        ),
        CandidateTarget(
            target_id="page:2:21",
            role="button",
            name="提交",
            text="提交",
            confidence=0.74,
            reasons=("重新观察",),
            recipe=LocatorRecipe(
                "dom_backend_node",
                role="button",
                name="提交",
                backend_node_id=21,
            ),
            box=BoundingBox(0, 0, 100, 30),
        ),
    )

    async def scenario() -> None:
        driver = ObservingDriver(
            [
                Observation(
                    surface_id="page",
                    url="https://example.com",
                    title="示例页面",
                    version=2,
                    fingerprint="obs-2",
                    summary="",
                    candidates=ambiguous,
                )
            ]
        )
        driver._candidate_cache[old_candidate.target_id] = old_candidate

        result = await driver.verify(ExpectedCondition("target_exists", old_candidate.target_id))

        assert result.success is False
        assert result.reason == "目标区域重新定位后出现歧义"

    asyncio.run(scenario())


def test_target_exists_reports_missing_when_relocation_fails(tmp_path: Path) -> None:
    class ObservingDriver(CdpAutomationDriver):
        def __init__(self, observations: list[Observation]) -> None:
            super().__init__(BrowserConfig(), tmp_path)
            self._observations = observations

        async def observe(self, *, force: bool = False) -> Observation:
            return self._observations.pop(0)

    old_candidate = CandidateTarget(
        target_id="page:1:10",
        role="button",
        name="提交",
        text="提交",
        confidence=0.95,
        reasons=("测试",),
        recipe=LocatorRecipe("ax_backend_node", role="button", name="提交", backend_node_id=10),
        box=BoundingBox(0, 0, 100, 30),
    )
    different = CandidateTarget(
        target_id="page:2:99",
        role="link",
        name="帮助中心",
        text="帮助中心",
        confidence=0.80,
        reasons=("重新观察",),
        recipe=LocatorRecipe("dom_backend_node", role="link", name="帮助中心", backend_node_id=99),
        box=BoundingBox(0, 0, 100, 30),
    )

    async def scenario() -> None:
        driver = ObservingDriver(
            [
                Observation(
                    surface_id="page",
                    url="https://example.com",
                    title="示例页面",
                    version=2,
                    fingerprint="obs-2",
                    summary="",
                    candidates=(different,),
                )
            ]
        )
        driver._candidate_cache[old_candidate.target_id] = old_candidate

        result = await driver.verify(ExpectedCondition("target_exists", old_candidate.target_id))

        assert result.success is False
        assert result.reason == "目标区域不存在"

    asyncio.run(scenario())

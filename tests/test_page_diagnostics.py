from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from witty_browser_auto.agent.tools import ToolExecutor
from witty_browser_auto.browser.diagnostics import CdpPageDiagnostics
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionReceipt,
    CandidateTarget,
    DragRiskClass,
    DriverCapabilities,
    ExecutionScope,
    LocatorRecipe,
    ModelToolCall,
    Observation,
    TaskSpec,
    VerificationResult,
)
from witty_browser_auto.security.redaction import REDACTED, TASK_INPUT_REDACTED


class FakeConnection:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def subscribe(self, method: str, handler: Any, *, session_id: str | None = None) -> Any:
        self.handlers[method] = handler

        def unsubscribe() -> None:
            self.handlers.pop(method, None)

        return unsubscribe


class FakeSession:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.session_id = "session-1"
        self.calls: list[str] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(method)
        return {}


def test_cdp_page_diagnostics_collects_bounded_runtime_and_network_signals() -> None:
    async def scenario() -> None:
        session = FakeSession()
        diagnostics = CdpPageDiagnostics(session)  # type: ignore[arg-type]
        await diagnostics.start()
        session.connection.handlers["Runtime.consoleAPICalled"](
            CdpEvent(
                "Runtime.consoleAPICalled",
                {"type": "error", "args": [{"type": "string", "value": "drag failed"}]},
                "session-1",
            )
        )
        session.connection.handlers["Runtime.exceptionThrown"](
            CdpEvent(
                "Runtime.exceptionThrown",
                {
                    "exceptionDetails": {
                        "text": "Uncaught",
                        "exception": {"className": "TypeError"},
                        "stackTrace": {
                            "callFrames": [
                                {
                                    "url": "https://example.com/app.js?token=secret",
                                    "functionName": "onPointerMove",
                                    "lineNumber": 42,
                                    "columnNumber": 7,
                                }
                            ]
                        },
                    }
                },
                "session-1",
            )
        )
        session.connection.handlers["Log.entryAdded"](
            CdpEvent(
                "Log.entryAdded",
                {
                    "entry": {
                        "source": "network",
                        "level": "error",
                        "text": "request rejected",
                        "url": "https://example.com/api?token=secret",
                    }
                },
                "session-1",
            )
        )

        snapshot = diagnostics.snapshot(
            page_state={
                "readyState": "complete",
                "visibilityState": "visible",
                "online": True,
                "hasFocus": True,
                "activeElement": {"tag": "div", "role": "slider", "disabled": False},
            },
            network_records=(
                {
                    "event": "response",
                    "url": "https://example.com/challenge?token=secret",
                    "status": 403,
                    "resource_type": "Fetch",
                },
            ),
            environment={"webdriver": False, "userAgent": "Chrome", "language": "zh-CN"},
            max_console=5,
            max_network=5,
        )

        assert {"frontend_exception", "console_error", "network_failure"} <= set(
            snapshot["signals"]
        )
        assert snapshot["page"]["activeElement"]["role"] == "slider"
        assert snapshot["network"]["responseStatusCounts"] == {"403": 1}
        assert "token=" in snapshot["network"]["failures"][0]["url"]
        assert REDACTED not in snapshot["network"]["failures"][0]["url"]
        assert "secret" not in str(snapshot)
        assert session.calls == ["Log.enable"]
        await diagnostics.close()
        assert not session.connection.handlers

    asyncio.run(scenario())


class DiagnosticDriver:
    capabilities = DriverCapabilities(dom=True, accessibility=True, network=True)

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root

    async def start(self) -> None:
        return None

    async def open(self, url: str) -> str:
        return "surface"

    async def observe(self, *, force: bool = False) -> Observation:
        return _observation()

    async def execute(self, command: ActionCommand) -> ActionReceipt:
        return ActionReceipt(command.action_id, True, True, "已执行", 1.0)

    async def verify(self, condition: object) -> VerificationResult:
        return VerificationResult(False, "页面状态未变化")

    async def capture_evidence(self, label: str) -> Path:
        return self.artifact_root / f"{label}.png"

    async def close(self) -> None:
        return None

    async def diagnostic_snapshot(
        self,
        *,
        max_console: int = 20,
        max_network: int = 30,
    ) -> dict[str, Any]:
        return {
            "signals": ["network_failure"],
            "console": ["account-value"],
            "limits": [max_console, max_network],
        }


def _observation() -> Observation:
    return Observation(
        surface_id="surface",
        url="https://example.com/slider",
        title="滑块",
        version=1,
        fingerprint="current",
        summary="普通业务滑块",
        candidates=(
            CandidateTarget(
                "slider",
                "slider",
                "进度",
                "",
                0.99,
                ("测试",),
                LocatorRecipe("test", role="slider", name="进度"),
                drag_risk=DragRiskClass.BUSINESS,
            ),
        ),
    )


def test_failed_action_automatically_attaches_page_diagnostics(tmp_path: Path) -> None:
    async def scenario() -> None:
        task = TaskSpec(
            "failure-diagnostics",
            "拖动普通业务滑块",
            "https://example.com/slider",
            ExecutionScope("project"),
            inputs={"account": "account-value"},
        )
        result = await ToolExecutor(  # type: ignore[arg-type]
            DiagnosticDriver(tmp_path), task
        ).execute(
            ModelToolCall(
                "drag-call",
                "drag",
                {
                    "target_id": "slider",
                    "end_dx": 120,
                    "end_dy": 0,
                    "duration_ms": 400,
                    "steps": 9,
                    "security_challenge": False,
                    "expect_kind": "text_contains",
                    "expect_value": "已完成",
                },
            ),
            _observation(),
        )

        assert result.success is False
        assert result.data["页面诊断"]["signals"] == ["network_failure"]
        assert result.data["页面诊断"]["console"] == [TASK_INPUT_REDACTED]
        assert result.data["页面诊断"]["limits"] == [12, 30]

    asyncio.run(scenario())

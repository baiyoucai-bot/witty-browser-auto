"""有界收集页面运行时、控制台和网络故障信号。"""

from __future__ import annotations

import logging
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.domain.errors import CdpCommandError
from witty_browser_auto.security.redaction import redact_url

logger = logging.getLogger(__name__)

DIAGNOSTIC_PAGE_SCRIPT = """
(()=>{
  const active = document.activeElement;
  return {
    readyState: document.readyState || '',
    visibilityState: document.visibilityState || '',
    online: navigator.onLine !== false,
    hasFocus: document.hasFocus(),
    activeElement: active ? {
      tag: (active.tagName || '').toLowerCase(),
      role: active.getAttribute('role') || '',
      type: active.getAttribute('type') || '',
      disabled: !!active.disabled || active.getAttribute('aria-disabled') === 'true',
      ariaBusy: active.getAttribute('aria-busy') || ''
    } : null
  };
})()
"""

_TEXT_LIMIT = 300


def _bounded_text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else f"{text[:limit]}..."


def _safe_remote_argument(argument: Any) -> str:
    if not isinstance(argument, Mapping):
        return ""
    value = argument.get("value")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _bounded_text(value)
    class_name = argument.get("className")
    if isinstance(class_name, str) and class_name:
        return f"<{class_name}>"
    return f"<{_bounded_text(argument.get('type', 'unknown'), 40)}>"


class CdpPageDiagnostics:
    """记录不会改变页面状态的 CDP 故障摘要。"""

    def __init__(self, session: CdpTargetSession, *, max_events: int = 200) -> None:
        self.session = session
        self.console_events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self.exception_events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self.log_events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._unsubscribers: list[Any] = []

    async def start(self) -> None:
        handlers = {
            "Runtime.consoleAPICalled": self._on_console,
            "Runtime.exceptionThrown": self._on_exception,
            "Log.entryAdded": self._on_log,
        }
        for method, handler in handlers.items():
            self._unsubscribers.append(
                self.session.connection.subscribe(
                    method,
                    handler,
                    session_id=self.session.session_id,
                )
            )
        try:
            await self.session.call("Log.enable")
        except CdpCommandError as exc:
            logger.debug(
                "当前页面不支持 CDP Log 域，继续采集 Runtime 诊断",
                extra={"cdp_method": exc.method, "cdp_error_code": exc.error_code},
            )

    async def close(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()

    def snapshot(
        self,
        *,
        page_state: Mapping[str, Any],
        network_records: Sequence[Mapping[str, Any]],
        environment: Mapping[str, Any],
        max_console: int,
        max_network: int,
    ) -> dict[str, Any]:
        network_failures = [
            self._safe_network_record(record)
            for record in network_records
            if record.get("event") == "failed"
            or (
                record.get("event") == "response"
                and isinstance(record.get("status"), int)
                and int(record["status"]) >= 400
            )
        ][-max_network:]
        statuses = Counter(
            str(record["status"])
            for record in network_records
            if record.get("event") == "response" and isinstance(record.get("status"), int)
        )
        console = [*self.console_events, *self.log_events]
        console.sort(key=lambda item: str(item.get("timestamp", "")))
        signals = self._classify(page_state, network_failures)
        return {
            "signals": signals,
            "page": self._safe_page_state(page_state),
            "environment": {
                "webdriver": environment.get("webdriver"),
                "language": _bounded_text(environment.get("language"), 40),
                "timezone": _bounded_text(environment.get("timezone"), 80),
                "visibilityState": _bounded_text(environment.get("visibilityState"), 40),
                "headlessUserAgent": "HeadlessChrome" in str(environment.get("userAgent", "")),
            },
            "console": console[-max_console:],
            "exceptions": list(self.exception_events)[-max_console:],
            "network": {
                "recordCount": len(network_records),
                "responseStatusCounts": dict(sorted(statuses.items())),
                "failures": network_failures,
            },
        }

    def _on_console(self, event: CdpEvent) -> None:
        arguments = event.params.get("args")
        previews = (
            [_safe_remote_argument(argument) for argument in arguments[:5]]
            if isinstance(arguments, list)
            else []
        )
        self.console_events.append(
            {
                "source": "runtime",
                "level": _bounded_text(event.params.get("type"), 40),
                "text": _bounded_text(" ".join(item for item in previews if item)),
                "timestamp": self._timestamp(event.params.get("timestamp")),
            }
        )

    def _on_exception(self, event: CdpEvent) -> None:
        details = event.params.get("exceptionDetails")
        if not isinstance(details, Mapping):
            return
        exception = details.get("exception")
        class_name = exception.get("className") if isinstance(exception, Mapping) else ""
        stack = details.get("stackTrace")
        frames = stack.get("callFrames") if isinstance(stack, Mapping) else None
        top_frame = frames[0] if isinstance(frames, list) and frames else {}
        self.exception_events.append(
            {
                "text": _bounded_text(details.get("text")),
                "class": _bounded_text(class_name, 80),
                "url": redact_url(str(top_frame.get("url", ""))),
                "function": _bounded_text(top_frame.get("functionName"), 100),
                "line": top_frame.get("lineNumber"),
                "column": top_frame.get("columnNumber"),
                "timestamp": self._timestamp(details.get("timestamp")),
            }
        )

    def _on_log(self, event: CdpEvent) -> None:
        entry = event.params.get("entry")
        if not isinstance(entry, Mapping):
            return
        self.log_events.append(
            {
                "source": _bounded_text(entry.get("source"), 40),
                "level": _bounded_text(entry.get("level"), 40),
                "text": _bounded_text(entry.get("text")),
                "url": redact_url(str(entry.get("url", ""))),
                "line": entry.get("lineNumber"),
                "timestamp": self._timestamp(entry.get("timestamp")),
            }
        )

    def _classify(
        self,
        page_state: Mapping[str, Any],
        network_failures: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        signals: list[str] = []
        if page_state.get("readyState") != "complete":
            signals.append("page_not_ready")
        if page_state.get("online") is False:
            signals.append("browser_offline")
        if page_state.get("visibilityState") == "hidden":
            signals.append("page_hidden")
        if self.exception_events:
            signals.append("frontend_exception")
        if any(
            item.get("level") in {"error", "warning"}
            for item in (*self.console_events, *self.log_events)
        ):
            signals.append("console_error")
        if network_failures:
            signals.append("network_failure")
        return signals or ["no_obvious_runtime_failure"]

    @staticmethod
    def _safe_page_state(page_state: Mapping[str, Any]) -> dict[str, Any]:
        active = page_state.get("activeElement")
        safe_active = (
            {
                key: active.get(key)
                for key in ("tag", "role", "type", "disabled", "ariaBusy")
                if isinstance(active, Mapping)
            }
            if isinstance(active, Mapping)
            else None
        )
        return {
            "readyState": _bounded_text(page_state.get("readyState"), 40),
            "visibilityState": _bounded_text(page_state.get("visibilityState"), 40),
            "online": page_state.get("online"),
            "hasFocus": page_state.get("hasFocus"),
            "activeElement": safe_active,
        }

    @staticmethod
    def _safe_network_record(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "event": record.get("event"),
            "url": redact_url(str(record.get("url", ""))),
            "status": record.get("status"),
            "resourceType": _bounded_text(record.get("resource_type"), 40),
            "reason": _bounded_text(record.get("failed_reason")),
            "timestamp": _bounded_text(record.get("timestamp"), 80),
        }

    @staticmethod
    def _timestamp(value: Any) -> str:
        if isinstance(value, (int, float)):
            return str(value)
        return datetime.now(UTC).isoformat()

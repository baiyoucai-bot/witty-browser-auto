"""只记录脱敏摘要的 CDP 网络观察器。"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.network.capture import CdpNetworkCapture
from witty_browser_auto.network.routing import CdpNetworkRouter
from witty_browser_auto.network.traffic import NetworkTrafficLog
from witty_browser_auto.security.redaction import redact, redact_url


@dataclass(frozen=True, slots=True)
class NetworkRecord:
    event: str
    request_id: str
    url: str
    method: str = ""
    status: int | None = None
    mime_type: str = ""
    resource_type: str = ""
    failed_reason: str = ""
    timestamp: str = ""
    headers: dict[str, Any] | None = None


class CdpNetworkRecorder:
    def __init__(
        self,
        session: CdpTargetSession,
        *,
        max_records: int = 1000,
        capture: CdpNetworkCapture | None = None,
        traffic: NetworkTrafficLog | None = None,
    ) -> None:
        self.session = session
        self.records: deque[NetworkRecord] = deque(maxlen=max_records)
        self.capture = capture
        self.traffic = traffic
        self.router = CdpNetworkRouter(session, tuple(capture.allowed_origins)) if capture else None
        self._request_urls: dict[str, str] = {}
        self._unsubscribers: list[Any] = []

    async def start(self, surface_id: str = "") -> None:
        methods: dict[str, Any] = {
            "Network.requestWillBeSent": self._on_request,
            "Network.responseReceived": self._on_response,
            "Network.loadingFinished": self._on_finished,
            "Network.loadingFailed": self._on_failed,
        }
        if self.traffic is not None:
            methods.update(self._traffic_only_handlers())
        for method, handler in methods.items():
            self._unsubscribers.append(
                self.session.connection.subscribe(
                    method,
                    handler,
                    session_id=self.session.session_id,
                )
            )
        if self.capture is not None and self.router is not None:
            await self.capture.bind_router(self.router)

    def _traffic_only_handlers(self) -> dict[str, Any]:
        """流量日志额外订阅的事件；extraInfo 提供浏览器实际收发的原始 Header。"""

        log = self.traffic
        assert log is not None
        return {
            "Network.requestWillBeSentExtraInfo": log.on_request_extra_info,
            "Network.responseReceivedExtraInfo": log.on_response_extra_info,
            "Network.webSocketCreated": log.on_websocket_created,
            "Network.webSocketWillSendHandshakeRequest": log.on_websocket_handshake_request,
            "Network.webSocketHandshakeResponseReceived": log.on_websocket_handshake_response,
            "Network.webSocketFrameSent": lambda event: log.on_websocket_frame(
                event, direction="sent"
            ),
            "Network.webSocketFrameReceived": lambda event: log.on_websocket_frame(
                event, direction="received"
            ),
            "Network.webSocketFrameError": log.on_websocket_error,
            "Network.webSocketClosed": log.on_websocket_closed,
            "Network.eventSourceMessageReceived": log.on_event_source_message,
        }

    async def snapshot(self) -> tuple[dict[str, Any], ...]:
        return tuple(asdict(record) for record in self.records)

    async def close(self) -> None:
        if self.capture is not None and self.router is not None:
            await self.capture.unbind_router(self.router)
        if self.router is not None:
            await self.router.close()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        self._request_urls.clear()
        if self.capture is not None:
            self.capture.discard_session(self.session.session_id)
        if self.traffic is not None:
            self.traffic.discard_session(self.session.session_id)

    def _on_request(self, event: CdpEvent) -> None:
        request = event.params.get("request")
        if not isinstance(request, dict):
            return
        if self.capture is not None:
            self.capture.observe_request(event)
        if self.traffic is not None:
            self.traffic.on_request(event)
        request_id = str(event.params.get("requestId", ""))
        safe_url = redact_url(str(request.get("url", "")))
        if request_id:
            self._request_urls[request_id] = safe_url
        self.records.append(
            NetworkRecord(
                event="request",
                request_id=request_id,
                url=safe_url,
                method=str(request.get("method", "")),
                resource_type=str(event.params.get("type", "")),
                timestamp=datetime.now(UTC).isoformat(),
                headers=redact(request.get("headers", {})),
            )
        )

    def _on_response(self, event: CdpEvent) -> None:
        response = event.params.get("response")
        if not isinstance(response, dict):
            return
        status = response.get("status")
        self.records.append(
            NetworkRecord(
                event="response",
                request_id=str(event.params.get("requestId", "")),
                url=redact_url(str(response.get("url", ""))),
                status=int(status) if isinstance(status, (int, float)) else None,
                mime_type=str(response.get("mimeType", "")),
                resource_type=str(event.params.get("type", "")),
                timestamp=datetime.now(UTC).isoformat(),
                headers=redact(response.get("headers", {})),
            )
        )
        if self.capture is not None:
            self.capture.observe_response(event)
        if self.traffic is not None:
            self.traffic.on_response(event)

    async def _on_finished(self, event: CdpEvent) -> None:
        try:
            if self.capture is not None:
                await self.capture.capture_finished(self.session, event)
            if self.traffic is not None:
                await self.traffic.on_finished(self.session, event)
        finally:
            self._request_urls.pop(str(event.params.get("requestId", "")), None)

    def _on_failed(self, event: CdpEvent) -> None:
        if self.capture is not None:
            self.capture.discard_request(event)
        if self.traffic is not None:
            self.traffic.on_failed(event)
        request_id = str(event.params.get("requestId", ""))
        reason_parts = [str(event.params.get("errorText", ""))]
        blocked_reason = event.params.get("blockedReason")
        if isinstance(blocked_reason, str) and blocked_reason:
            reason_parts.append(f"blocked:{blocked_reason}")
        cors_status = event.params.get("corsErrorStatus")
        if isinstance(cors_status, dict) and cors_status.get("corsError"):
            reason_parts.append(f"cors:{cors_status['corsError']}")
        self.records.append(
            NetworkRecord(
                event="failed",
                request_id=request_id,
                url=self._request_urls.pop(request_id, ""),
                resource_type=str(event.params.get("type", "")),
                failed_reason="; ".join(part for part in reason_parts if part),
                timestamp=datetime.now(UTC).isoformat(),
            )
        )

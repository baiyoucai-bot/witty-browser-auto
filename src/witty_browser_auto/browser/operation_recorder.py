"""通过 CDP 记录页面交互，供工作台展示和后续路径学习。"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.security.redaction import redact_url

logger = logging.getLogger(__name__)

BrowserOperationSink = Callable[[dict[str, Any]], Awaitable[None] | None]
_BINDING_NAME = "__wittyRecordOperation"
_INSTALL_SCRIPT = r"""
(() => {
  if (globalThis.__wittyOperationRecorderInstalled) return;
  globalThis.__wittyOperationRecorderInstalled = true;
  globalThis.__wittyAgentActionActive = false;
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const stableAttributes = element => {
    const output = {};
    for (const name of ['data-testid', 'id', 'name', 'aria-label']) {
      const value = element.getAttribute && element.getAttribute(name);
      if (value) output[name] = clean(value);
    }
    return output;
  };
  const record = event => {
    const raw = event.target;
    if (!(raw instanceof Element)) return;
    const element = raw.closest('button,a,input,select,textarea,[role],form') || raw;
    const tag = clean(element.tagName).toLowerCase();
    const hiddenTextTags = new Set(['form', 'input', 'select', 'textarea']);
    const payload = {
      kind: event.type,
      source: globalThis.__wittyAgentActionActive ? 'agent' : 'user',
      trusted: event.isTrusted === true,
      url: location.href,
      tag,
      role: clean(element.getAttribute && element.getAttribute('role')),
      input_type: tag === 'input' ? clean(element.getAttribute('type')) : '',
      text: hiddenTextTags.has(tag) ? '' : clean(element.innerText || element.textContent),
      attributes: stableAttributes(element),
    };
    try { globalThis.__wittyRecordOperation(JSON.stringify(payload)); } catch (_) {}
  };
  for (const kind of ['click', 'change', 'submit']) {
    document.addEventListener(kind, record, true);
  }
})();
"""


class CdpUserOperationRecorder:
    def __init__(self, sink: BrowserOperationSink | None) -> None:
        self.sink = sink
        self.session: CdpTargetSession | None = None
        self._unsubscribe: Callable[[], None] | None = None

    async def start(self, session: CdpTargetSession) -> None:
        await self.close()
        self.session = session
        if self.sink is None:
            return
        self._unsubscribe = session.connection.subscribe(
            "Runtime.bindingCalled",
            self._on_binding_called,
            session_id=session.session_id,
        )
        await session.call("Runtime.addBinding", {"name": _BINDING_NAME})
        await session.call(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _INSTALL_SCRIPT},
        )
        await session.call(
            "Runtime.evaluate",
            {"expression": _INSTALL_SCRIPT, "returnByValue": True},
        )

    async def set_agent_action(self, active: bool) -> None:
        if self.sink is None or self.session is None:
            return
        await self.session.call(
            "Runtime.evaluate",
            {
                "expression": (f"globalThis.__wittyAgentActionActive={str(active).lower()}"),
                "returnByValue": True,
            },
        )

    async def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self.session = None

    async def _on_binding_called(self, event: CdpEvent) -> None:
        if event.params.get("name") != _BINDING_NAME or self.sink is None:
            return
        raw_payload = event.params.get("payload")
        if not isinstance(raw_payload, str):
            return
        try:
            decoded = json.loads(raw_payload)
        except json.JSONDecodeError:
            logger.warning("浏览器操作记录载荷不是有效 JSON")
            return
        operation = _sanitize_operation(decoded)
        if operation is None:
            return
        result = self.sink(operation)
        if inspect.isawaitable(result):
            await result


def _sanitize_operation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    kind = value.get("kind")
    if kind not in {"click", "change", "submit"}:
        return None
    attributes = value.get("attributes")
    safe_attributes = (
        {
            str(key)[:40]: str(item)[:120]
            for key, item in attributes.items()
            if key in {"data-testid", "id", "name", "aria-label"} and isinstance(item, str)
        }
        if isinstance(attributes, Mapping)
        else {}
    )
    url = value.get("url")
    return {
        "kind": kind,
        "source": "agent" if value.get("source") == "agent" else "user",
        "trusted": value.get("trusted") is True,
        "url": redact_url(url) if isinstance(url, str) else "",
        "tag": _bounded(value.get("tag"), 24),
        "role": _bounded(value.get("role"), 60),
        "input_type": _bounded(value.get("input_type"), 30),
        "text": _bounded(value.get("text"), 120),
        "attributes": safe_attributes,
        "recorded_at": datetime.now(UTC).isoformat(),
    }


def _bounded(value: Any, limit: int) -> str:
    return str(value).strip()[:limit] if isinstance(value, str) else ""

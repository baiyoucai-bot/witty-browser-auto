"""页面性能采集：Core Web Vitals、导航计时与资源概览。

一条由真实 Chrome 探测钉死的规则：**LCP 必须在导航之前挂上观察器**。导航结束后再
`new PerformanceObserver(...).observe({buffered: true})`，FCP 能从缓冲区补回来
，实测 140ms，，LCP 拿到的却是 `null`。所以要测一次真实的加载性能，就必须用
`Page.addScriptToEvaluateOnNewDocument` 先把采集器注入，再触发一次导航。

`measure` 因此分两种模式：`reload=True` 走"注入 + 重载"拿到完整口径；`reload=False`
只报当前页面已经能拿到的部分，并明确告诉调用方 LCP 为什么缺席，而不是悄悄返回 0。
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

# 阈值取自 Google 对 Core Web Vitals 的公开口径，单位毫秒，CLS 无量纲。
VITAL_THRESHOLDS: dict[str, tuple[float, float]] = {
    "lcp": (2500, 4000),
    "fcp": (1800, 3000),
    "inp": (200, 500),
    "cls": (0.1, 0.25),
    "ttfb": (800, 1800),
}

COLLECTOR_SCRIPT = """
(() => {
  if (window.__wittyVitals) return;
  const state = {lcp: null, cls: 0, fcp: null, inp: null, installedAt: Date.now()};
  window.__wittyVitals = state;
  const observe = (type, handler) => {
    try {
      new PerformanceObserver((list) => list.getEntries().forEach(handler))
        .observe({type, buffered: true});
    } catch (error) { /* 该类型不受支持时跳过，不影响其余指标 */ }
  };
  observe('largest-contentful-paint', (entry) => { state.lcp = entry.startTime; });
  observe('paint', (entry) => {
    if (entry.name === 'first-contentful-paint') state.fcp = entry.startTime;
  });
  observe('layout-shift', (entry) => {
    if (!entry.hadRecentInput) state.cls += entry.value;
  });
  observe('event', (entry) => {
    if (entry.interactionId && (state.inp === null || entry.duration > state.inp)) {
      state.inp = entry.duration;
    }
  });
})();
"""

_READ_SCRIPT = """
(() => {
  const vitals = window.__wittyVitals || null;
  const navigation = performance.getEntriesByType('navigation')[0] || null;
  const resources = performance.getEntriesByType('resource') || [];
  const byType = {};
  let transferred = 0;
  for (const item of resources) {
    const kind = item.initiatorType || 'other';
    byType[kind] = (byType[kind] || 0) + 1;
    transferred += item.transferSize || 0;
  }
  const slowest = resources
    .slice()
    .sort((a, b) => b.duration - a.duration)
    .slice(0, 5)
    .map((item) => ({
      name: String(item.name).slice(0, 200),
      duration_ms: Math.round(item.duration),
      transfer_bytes: item.transferSize || 0,
      type: item.initiatorType || 'other',
    }));
  return {
    vitals,
    navigation: navigation ? {
      ttfb_ms: navigation.responseStart,
      dom_content_loaded_ms: navigation.domContentLoadedEventEnd,
      load_ms: navigation.loadEventEnd,
      dom_interactive_ms: navigation.domInteractive,
      transfer_bytes: navigation.transferSize || 0,
      redirect_count: navigation.redirectCount || 0,
    } : null,
    resources: {
      count: resources.length,
      transfer_bytes: transferred,
      by_type: byType,
      slowest,
    },
  };
})()
"""

_COUNTER_KEYS = {
    "Nodes": "dom_nodes",
    "LayoutObjects": "layout_objects",
    "JSEventListeners": "js_event_listeners",
    "Documents": "documents",
    "Frames": "frames",
    "Resources": "resources",
}


class PerformanceSession(Protocol):
    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...


async def install_collector(session: PerformanceSession) -> None:
    """把采集器注册为新文档脚本，使其早于页面自身代码运行。"""

    await session.call("Page.addScriptToEvaluateOnNewDocument", {"source": COLLECTOR_SCRIPT})


async def read_metrics(
    session: PerformanceSession,
    *,
    settle_seconds: float = 0.0,
) -> dict[str, Any]:
    """读取当前页面的性能数据。"""

    if settle_seconds > 0:
        await asyncio.sleep(settle_seconds)
    result = await session.call(
        "Runtime.evaluate", {"expression": _READ_SCRIPT, "returnByValue": True}
    )
    payload = result.get("result", {}).get("value")
    if not isinstance(payload, dict):
        raise RuntimeError("页面未返回性能数据")

    vitals = payload.get("vitals") if isinstance(payload.get("vitals"), dict) else {}
    navigation = payload.get("navigation") if isinstance(payload.get("navigation"), dict) else {}
    metrics: dict[str, Any] = {
        "lcp_ms": _rounded(vitals.get("lcp")),
        "fcp_ms": _rounded(vitals.get("fcp")),
        "cls": round(vitals["cls"], 4) if isinstance(vitals.get("cls"), int | float) else None,
        "inp_ms": _rounded(vitals.get("inp")),
        "ttfb_ms": _rounded(navigation.get("ttfb_ms")),
    }
    return {
        "core_web_vitals": metrics,
        "ratings": {name: rate(name, value) for name, value in _vital_pairs(metrics)},
        "navigation": {
            key: _rounded(value) if key.endswith("_ms") else value
            for key, value in navigation.items()
        },
        "resources": payload.get("resources") or {},
        "collector_installed": bool(vitals),
    }


async def read_counters(session: PerformanceSession) -> dict[str, Any]:
    """读取运行时计数器；DOM 规模异常往往先于卡顿出现。"""

    await session.call("Performance.enable")
    result = await session.call("Performance.getMetrics")
    counters: dict[str, Any] = {}
    for item in result.get("metrics", []):
        if not isinstance(item, dict):
            continue
        key = _COUNTER_KEYS.get(str(item.get("name")))
        if key is not None:
            counters[key] = item.get("value")
    return counters


def rate(name: str, value: float | None) -> str:
    """按 Google 公开阈值给出好/需改进/差；拿不到值时明说未知。"""

    if value is None:
        return "unknown"
    thresholds = VITAL_THRESHOLDS.get(name)
    if thresholds is None:
        return "unknown"
    good, poor = thresholds
    if value <= good:
        return "good"
    if value <= poor:
        return "needs_improvement"
    return "poor"


def _vital_pairs(metrics: dict[str, Any]) -> list[tuple[str, float | None]]:
    return [
        ("lcp", metrics.get("lcp_ms")),
        ("fcp", metrics.get("fcp_ms")),
        ("inp", metrics.get("inp_ms")),
        ("cls", metrics.get("cls")),
        ("ttfb", metrics.get("ttfb_ms")),
    ]


def _rounded(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return round(float(value), 1)
    return None

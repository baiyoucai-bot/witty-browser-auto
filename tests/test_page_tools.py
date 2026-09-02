"""元素拖放、PDF 导出与性能采集的单元测试。"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from witty_browser_auto.agent.page_tools import (
    execute_drag_to_element,
    execute_measure_performance,
    execute_save_pdf,
)
from witty_browser_auto.browser.drag_drop import drag_between_points
from witty_browser_auto.browser.page_export import build_print_params, export_pdf
from witty_browser_auto.browser.performance import rate, read_metrics
from witty_browser_auto.domain.models import (
    CandidateTarget,
    DragRiskClass,
    DriverCapabilities,
    LocatorRecipe,
    Observation,
)


def _run(coro):
    return asyncio.run(coro)


def _observation(
    *,
    page_risk: DragRiskClass = DragRiskClass.BUSINESS,
    candidates: tuple[CandidateTarget, ...] = (),
) -> Observation:
    return Observation(
        surface_id="s",
        url="https://board.test/",
        title="看板",
        version=1,
        fingerprint="fp-1",
        summary="",
        candidates=candidates,
        visual_drag_risk=page_risk,
    )


def _plain_observation() -> Observation:
    return _observation()


def _challenge_observation(*, source_is_challenge: bool = False) -> Observation:
    if not source_is_challenge:
        return _observation(page_risk=DragRiskClass.SECURITY)
    return _observation(
        candidates=(
            CandidateTarget(
                target_id="slider",
                role="slider",
                name="拖动滑块完成验证",
                text="",
                reasons=(),
                recipe=LocatorRecipe(strategy="css", value="#slider"),
                confidence=0.9,
                drag_risk=DragRiskClass.SECURITY,
            ),
        ),
    )


class _DragSession:
    """记录派发顺序的假会话；可选地模拟原生拖放截获。"""

    session_id = "s-1"

    def __init__(self, *, intercept: bool = False) -> None:
        self.events: list[tuple[str, str]] = []
        self.intercept = intercept
        self._handler = None
        self.connection = self

    def subscribe(self, method, handler, *, session_id=None):
        self._handler = handler
        return lambda: None

    async def call(self, method, params=None, *, timeout_seconds=None):
        params = dict(params or {})
        if method == "Input.dispatchMouseEvent":
            self.events.append((method, str(params.get("type"))))
            # 浏览器在按下并移动之后才判定为原生拖拽。
            if self.intercept and params.get("type") == "mouseMoved" and params.get("buttons"):
                if self._handler is not None:
                    event = type(
                        "E",
                        (),
                        {
                            "params": {
                                "data": {"items": [{"mimeType": "text/plain", "data": "payload"}]}
                            }
                        },
                    )()
                    self._handler(event)
        elif method == "Input.dispatchDragEvent":
            self.events.append((method, str(params.get("type"))))
        else:
            self.events.append((method, str(params.get("enabled", ""))))
        return {}

    def kinds(self, method: str) -> list[str]:
        return [kind for name, kind in self.events if name == method]


def test_native_drag_completes_through_drag_events() -> None:
    session = _DragSession(intercept=True)
    outcome = _run(drag_between_points(session, (10, 10), (200, 200)))
    assert outcome["channel"] == "html5"
    assert outcome["mime_types"] == ["text/plain"]
    # 原生通道必须补 drop，纯鼠标事件永远不会触发它。
    assert "drop" in session.kinds("Input.dispatchDragEvent")
    assert "mouseReleased" not in session.kinds("Input.dispatchMouseEvent")


def test_pointer_drag_is_used_when_nothing_is_intercepted() -> None:
    session = _DragSession(intercept=False)
    outcome = _run(drag_between_points(session, (10, 10), (200, 200), steps=6, step_delay_ms=0))
    assert outcome["channel"] == "pointer"
    kinds = session.kinds("Input.dispatchMouseEvent")
    assert kinds[0] == "mouseMoved"
    assert "mousePressed" in kinds
    assert kinds[-1] == "mouseReleased"
    assert not session.kinds("Input.dispatchDragEvent")


def test_interception_is_always_turned_off_again() -> None:
    session = _DragSession(intercept=True)
    _run(drag_between_points(session, (0, 0), (50, 50)))
    toggles = [value for name, value in session.events if name == "Input.setInterceptDrags"]
    # 留着截获开关会让后续所有拖拽都被吞掉。
    assert toggles == ["True", "False"]


def test_drag_falls_back_to_pointer_when_interception_is_unsupported() -> None:
    class _NoIntercept(_DragSession):
        async def call(self, method, params=None, *, timeout_seconds=None):
            if method == "Input.setInterceptDrags":
                raise RuntimeError("不支持")
            return await super().call(method, params, timeout_seconds=timeout_seconds)

    session = _NoIntercept()
    outcome = _run(drag_between_points(session, (0, 0), (80, 80), steps=4, step_delay_ms=0))
    assert outcome["channel"] == "pointer"


class _DragDriver:
    def __init__(self, risk: str = "business") -> None:
        self.capabilities = DriverCapabilities(element_drag=True, pdf_export=True, performance=True)
        self.risk = risk
        self.received: dict = {}

    async def drag_to_element(self, **kwargs):
        self.received = kwargs
        return {
            "source": "第 3 行",
            "target": "已完成列",
            "source_risk": self.risk,
            "channel": "html5",
            "mime_types": ["text/plain"],
        }


def test_drag_to_element_accepts_both_endpoint_forms() -> None:
    driver = _DragDriver()
    outcome = _run(
        execute_drag_to_element(
            {
                "source_target_id": "a",
                "target_locator": {"strategy": "css", "value": "#done"},
            },
            driver=driver,
        )
    )
    assert outcome.success is True
    assert driver.received["source_target_id"] == "a"
    assert driver.received["target_locator"].strategy == "explicit_css"
    assert "#done" in driver.received["target_locator"].value
    assert "原生拖放" in outcome.message


def test_drag_to_element_refuses_security_challenge_sources() -> None:
    # 验证码滑块必须走 drag / visual_drag，那里才有截图留证与尝试预算。
    with pytest.raises(ValueError, match="安全挑战"):
        _run(
            execute_drag_to_element(
                {"source_target_id": "a", "target_target_id": "b"},
                driver=_DragDriver(),
                observation=_challenge_observation(),
            )
        )


def test_drag_to_element_refuses_a_challenge_classified_source() -> None:
    with pytest.raises(ValueError, match="源元素疑似安全挑战"):
        _run(
            execute_drag_to_element(
                {"source_target_id": "slider", "target_target_id": "b"},
                driver=_DragDriver(),
                observation=_challenge_observation(source_is_challenge=True),
            )
        )


def test_drag_to_element_allows_unclassified_sources() -> None:
    # 定位器解析出的候选风险恒为 unknown，看板这类页面往往连候选都没有；
    # 把 unknown 当拒绝理由会让本工具在它主要服务的场景上完全不可用。
    outcome = _run(
        execute_drag_to_element(
            {
                "source_locator": {"strategy": "test_id", "value": "card-1"},
                "target_locator": {"strategy": "test_id", "value": "col-done"},
            },
            driver=_DragDriver(risk="unknown"),
            observation=_plain_observation(),
        )
    )
    assert outcome.success is True


def test_drag_to_element_rejects_ambiguous_endpoints() -> None:
    driver = _DragDriver()
    with pytest.raises(ValueError, match="source_target_id 或 source_locator"):
        _run(execute_drag_to_element({"target_target_id": "b"}, driver=driver))
    with pytest.raises(ValueError, match="target_target_id 或 target_locator"):
        _run(
            execute_drag_to_element(
                {
                    "source_target_id": "a",
                    "target_target_id": "b",
                    "target_locator": {"strategy": "css", "value": "#x"},
                },
                driver=driver,
            )
        )
    with pytest.raises(ValueError, match="steps 必须在 4 到 60"):
        _run(
            execute_drag_to_element(
                {"source_target_id": "a", "target_target_id": "b", "steps": 999},
                driver=driver,
            )
        )


# ----------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------


class _PdfSession:
    def __init__(self, payload: bytes = b"%PDF-1.4 fake") -> None:
        self.payload = payload
        self.params: dict = {}

    async def call(self, method, params=None, *, timeout_seconds=None):
        self.params = dict(params or {})
        return {"data": base64.b64encode(self.payload).decode()}


def test_pdf_is_written_as_a_private_file(tmp_path: Path) -> None:
    session = _PdfSession()
    outcome = _run(export_pdf(session, tmp_path / "pdf", label="对账单"))
    path = Path(outcome["pdf_path"])
    assert path.is_file()
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert path.read_bytes().startswith(b"%PDF")
    assert path.name.startswith("对账单-")


def test_pdf_label_cannot_escape_the_directory(tmp_path: Path) -> None:
    outcome = _run(export_pdf(_PdfSession(), tmp_path / "pdf", label="../../etc/passwd"))
    path = Path(outcome["pdf_path"])
    # 分隔符与点号被剔除，文件只能落在指定目录内。
    assert path.parent == tmp_path / "pdf"
    assert "/" not in path.name and ".." not in path.name


def test_non_pdf_payload_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="不是 PDF"):
        _run(export_pdf(_PdfSession(b"<html>error</html>"), tmp_path))


def test_print_params_validate_their_ranges() -> None:
    params = build_print_params(paper="letter", landscape=True, page_ranges="1-3,5")
    assert params["paperWidth"] == 8.5
    assert params["landscape"] is True
    assert params["pageRanges"] == "1-3,5"
    with pytest.raises(ValueError, match="不支持的纸张"):
        build_print_params(paper="a9")
    with pytest.raises(ValueError, match="scale"):
        build_print_params(scale=5)
    with pytest.raises(ValueError, match="page_ranges"):
        build_print_params(page_ranges="第一页")


def test_save_pdf_reports_size(tmp_path: Path) -> None:
    class _Driver:
        capabilities = DriverCapabilities(pdf_export=True)

        async def save_page_pdf(self, *, label, params):
            return {"pdf_path": str(tmp_path / "a.pdf"), "bytes": 2048}

    outcome = _run(execute_save_pdf({"label": "bill"}, driver=_Driver()))
    assert "2.0 KB" in outcome.message


# ----------------------------------------------------------------------
# 性能
# ----------------------------------------------------------------------


class _PerfSession:
    def __init__(self, payload) -> None:
        self.payload = payload

    async def call(self, method, params=None, *, timeout_seconds=None):
        if method == "Runtime.evaluate":
            return {"result": {"value": self.payload}}
        if method == "Performance.getMetrics":
            return {
                "metrics": [
                    {"name": "Nodes", "value": 812},
                    {"name": "JSEventListeners", "value": 41},
                    {"name": "Unrelated", "value": 1},
                ]
            }
        return {}


def test_metrics_are_rated_against_published_thresholds() -> None:
    session = _PerfSession(
        {
            "vitals": {"lcp": 1800.4, "fcp": 900.0, "cls": 0.0312, "inp": 320.0},
            "navigation": {"ttfb_ms": 2400.0, "load_ms": 3000.0},
            "resources": {"count": 12, "transfer_bytes": 4096, "by_type": {}, "slowest": []},
        }
    )
    metrics = _run(read_metrics(session))
    assert metrics["core_web_vitals"]["lcp_ms"] == 1800.4
    assert metrics["core_web_vitals"]["cls"] == 0.0312
    assert metrics["ratings"]["lcp"] == "good"
    assert metrics["ratings"]["cls"] == "good"
    assert metrics["ratings"]["inp"] == "needs_improvement"
    assert metrics["ratings"]["ttfb"] == "poor"


def test_missing_vitals_are_unknown_rather_than_zero() -> None:
    session = _PerfSession({"vitals": None, "navigation": None, "resources": {}})
    metrics = _run(read_metrics(session))
    assert metrics["core_web_vitals"]["lcp_ms"] is None
    # 缺席必须显式为 unknown，报成 good 会让调用方以为页面很快。
    assert metrics["ratings"]["lcp"] == "unknown"
    assert metrics["collector_installed"] is False


def test_rate_boundaries() -> None:
    assert rate("lcp", 2500) == "good"
    assert rate("lcp", 2500.1) == "needs_improvement"
    assert rate("lcp", 4000.1) == "poor"
    assert rate("unknown_metric", 1) == "unknown"


class _PerfDriver:
    capabilities = DriverCapabilities(performance=True)

    def __init__(self, lcp=None) -> None:
        self.lcp = lcp
        self.reloaded = None

    async def measure_performance(self, *, reload_page, settle_seconds):
        self.reloaded = reload_page
        return {
            "core_web_vitals": {"lcp_ms": self.lcp, "cls": 0.02, "ttfb_ms": 120.0},
            "ratings": {"lcp": "good" if self.lcp else "unknown", "cls": "good", "ttfb": "good"},
            "navigation": {},
            "resources": {},
            "counters": {},
        }


def test_missing_lcp_explains_that_a_reload_is_required() -> None:
    outcome = _run(execute_measure_performance({}, driver=_PerfDriver()))
    # 探测确认导航后安装的采集器连 buffered 也补不回 LCP，必须说清楚。
    assert "reload=true" in outcome.message


def test_reload_mode_reports_lcp_without_the_caveat() -> None:
    driver = _PerfDriver(lcp=1200.0)
    outcome = _run(execute_measure_performance({"reload": True}, driver=driver))
    assert driver.reloaded is True
    assert "reload=true" not in outcome.message
    assert "LCP 1200.0ms" in outcome.message


def test_measure_performance_rejects_bad_arguments() -> None:
    driver = _PerfDriver()
    with pytest.raises(ValueError, match="reload 必须是布尔值"):
        _run(execute_measure_performance({"reload": "yes"}, driver=driver))
    with pytest.raises(ValueError, match="settle_seconds 必须在 0 到 30"):
        _run(execute_measure_performance({"settle_seconds": 99}, driver=driver))
    with pytest.raises(ValueError, match="未知参数"):
        _run(execute_measure_performance({"nope": 1}, driver=driver))

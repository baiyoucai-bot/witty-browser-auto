"""编号标注截图的回归。

这个工具的全部价值在于"图上的数字能对回 target_id"：编号与图例必须严格一致；视口外的
候选不能进图例，因为图上根本看不到；覆盖层必须用后必除，留下就污染后续截图与用户视野。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from witty_browser_auto.agent.tools import ToolExecutor
from witty_browser_auto.browser.annotation import (
    CONTAINER_ID,
    MAX_LABELS,
    build_annotation_labels,
    drawn_labels,
    overlay_payload,
)
from witty_browser_auto.domain.models import (
    ActionReceipt,
    BoundingBox,
    CandidateTarget,
    DriverCapabilities,
    ExecutionScope,
    ExpectedCondition,
    LocatorRecipe,
    ModelToolCall,
    Observation,
    TaskSpec,
    VerificationResult,
)


def _candidate(
    target_id: str,
    *,
    role: str = "button",
    name: str = "提交",
    confidence: float = 0.9,
    box: tuple[float, float, float, float] | None = (10.0, 20.0, 80.0, 30.0),
) -> CandidateTarget:
    return CandidateTarget(
        target_id,
        role,
        name,
        "文本",
        confidence,
        ("测试",),
        LocatorRecipe("css", value=f"#{target_id}"),
        BoundingBox(*box) if box is not None else None,
    )


# ----------------------------------------------------------------------
# 纯函数：编号与图例
# ----------------------------------------------------------------------


def test_labels_start_at_one_and_follow_confidence() -> None:
    labels = build_annotation_labels(
        (
            _candidate("t-low", confidence=0.2),
            _candidate("t-high", confidence=0.95),
            _candidate("t-mid", confidence=0.6),
        )
    )
    assert [item.label for item in labels] == [1, 2, 3]
    # 编号顺序必须与置信度顺序一致，截断时才不会把真正的目标丢掉。
    assert [item.target_id for item in labels] == ["t-high", "t-mid", "t-low"]


def test_candidates_without_a_visible_box_are_skipped() -> None:
    labels = build_annotation_labels(
        (
            _candidate("t-none", box=None),
            _candidate("t-zero", box=(0.0, 0.0, 0.0, 0.0)),
            _candidate("t-ok"),
        )
    )
    assert [item.target_id for item in labels] == ["t-ok"]


def test_role_filter_and_label_budget() -> None:
    candidates = (
        _candidate("t-btn", role="button"),
        _candidate("t-input", role="textbox"),
    )
    assert [item.target_id for item in build_annotation_labels(candidates, roles=("textbox",))] == [
        "t-input"
    ]

    many = tuple(_candidate(f"t-{index}", confidence=index / 100) for index in range(1, 11))
    assert len(build_annotation_labels(many, max_labels=4)) == 4
    with pytest.raises(ValueError, match="标注数量"):
        build_annotation_labels(many, max_labels=MAX_LABELS + 1)


def test_overlay_payload_only_carries_structured_geometry() -> None:
    payload = overlay_payload(build_annotation_labels((_candidate("t-1"),)))
    assert payload["containerId"] == CONTAINER_ID
    assert payload["labels"] == [{"label": 1, "x": 10.0, "y": 20.0, "width": 80.0, "height": 30.0}]
    # 页面脚本只接受结构化数据，绝不能带上可执行表达式。
    assert json.dumps(payload)


def test_drawn_labels_tolerates_missing_or_bad_payloads() -> None:
    assert drawn_labels({"drawn": [1, 3]}) == (1, 3)
    assert drawn_labels({"drawn": "nope"}) == ()
    assert drawn_labels(None) == ()


# ----------------------------------------------------------------------
# 执行层：图例、清理与失败语义
# ----------------------------------------------------------------------


class _AnnotationDriver:
    """记录脚本调用顺序的假驱动；只关心"画了、截了、清了"。"""

    capabilities = DriverCapabilities(dom=True, accessibility=True, javascript=True)

    def __init__(
        self,
        artifact_root: Path,
        *,
        drawn: list[int] | None = None,
        fail_capture: bool = False,
    ) -> None:
        self.artifact_root = artifact_root
        self.drawn = drawn
        self.fail_capture = fail_capture
        self.scripts: list[str] = []
        self.candidates: tuple[CandidateTarget, ...] = (_candidate("t-1"), _candidate("t-2"))

    async def observe(self, *, force: bool = False) -> Observation:
        return Observation(
            surface_id="surface-1",
            url="https://shop.test/",
            title="首页",
            version=1,
            fingerprint="fp",
            summary="首页",
            candidates=self.candidates,
        )

    async def capture_annotated_screenshot(
        self,
        labels: Any,
        *,
        label: str = "annotated",
    ) -> dict[str, Any]:
        # 真实驱动的绘制-截图-清理顺序由 driver 保证，这里只模拟其返回契约。
        self.scripts.append("draw")
        if self.fail_capture:
            self.scripts.append("cleanup")
            raise RuntimeError("浏览器未返回截图数据")
        self.scripts.append("capture")
        self.scripts.append("cleanup")
        path = self.artifact_root / f"{label}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        drawn = self.drawn if self.drawn is not None else [item.label for item in labels]
        return {"screenshot_path": str(path), "drawn": drawn}

    async def execute(self, command: Any) -> ActionReceipt:
        raise AssertionError("标注截图不应派发任何页面动作")

    async def verify(self, condition: ExpectedCondition) -> VerificationResult:
        return VerificationResult(True, "ok")

    async def capture_evidence(self, label: str) -> Path:
        raise AssertionError("标注截图走自己的截图路径")


def _executor(driver: Any) -> ToolExecutor:
    return ToolExecutor(
        driver,
        TaskSpec("annotate", "看图操作", "https://shop.test/", ExecutionScope("project")),
    )


def _run(executor: ToolExecutor, driver: Any, arguments: dict[str, Any] | None = None) -> Any:
    async def scenario() -> Any:
        observation = await driver.observe()
        return await executor.execute(
            ModelToolCall("c1", "capture_annotated_screenshot", arguments or {}),
            observation,
        )

    return asyncio.run(scenario())


def test_legend_maps_labels_back_to_target_ids(tmp_path: Path) -> None:
    driver = _AnnotationDriver(tmp_path)
    result = _run(_executor(driver), driver)

    assert result.success is True
    assert result.counts_as_action is False
    legend = result.data["legend"]
    assert [item["label"] for item in legend] == [1, 2]
    assert {item["target_id"] for item in legend} == {"t-1", "t-2"}
    assert legend[0]["box"] == {"x": 10.0, "y": 20.0, "width": 80.0, "height": 30.0}
    assert Path(result.data["screenshot_path"]).exists()
    assert result.evidence is not None and result.evidence.kind == "annotated_screenshot"
    # 绘制、截图、清理三步都要发生。
    assert driver.scripts == ["draw", "capture", "cleanup"]


def test_labels_outside_the_viewport_stay_out_of_the_legend(tmp_path: Path) -> None:
    # 只有编号 2 真的被画上去，图例不能出现图上看不见的编号 1。
    driver = _AnnotationDriver(tmp_path, drawn=[2])
    result = _run(_executor(driver), driver)

    legend = result.data["legend"]
    assert [item["label"] for item in legend] == [2]
    assert result.data["annotated_count"] == 1
    assert result.data["candidate_count"] == 2


def test_all_candidates_off_screen_is_a_business_failure(tmp_path: Path) -> None:
    driver = _AnnotationDriver(tmp_path, drawn=[])
    result = _run(_executor(driver), driver)

    assert result.success is False
    assert "视口之外" in result.message
    assert result.data["legend"] == []


def test_no_annotatable_candidate_reports_without_touching_the_page(tmp_path: Path) -> None:
    driver = _AnnotationDriver(tmp_path)
    driver.candidates = (_candidate("t-hidden", box=None),)
    result = _run(_executor(driver), driver)

    assert result.success is False
    assert "可见矩形" in result.message
    # 没有可标注候选时不该白跑一趟绘制与截图。
    assert driver.scripts == []


def test_model_view_hides_the_screenshot_path(tmp_path: Path) -> None:
    driver = _AnnotationDriver(tmp_path)
    result = _run(_executor(driver), driver)

    assert result.model_data is not None
    serialized = json.dumps(result.model_data, ensure_ascii=False)
    assert result.data["screenshot_path"] not in serialized
    # 编号与 target_id 必须留给模型，否则看了图也没法操作。
    assert result.model_data["legend"][0]["target_id"] == "t-1"

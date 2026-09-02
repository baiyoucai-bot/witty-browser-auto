"""观察与工具结果转模型上下文的单元测试。

外部 harness 自带 LLM，这条转换路径是它把页面状态与工具结果送进模型的唯一出口：
必须可 `json.dumps`、必须带 token 预算、必须显式标注截断。
"""

from __future__ import annotations

import json

from witty_browser_auto.agent.tools import ToolExecutionResult
from witty_browser_auto.domain.models import (
    ActionReceipt,
    BoundingBox,
    CandidateTarget,
    DragRiskClass,
    EvidenceRef,
    LocatorRecipe,
    Observation,
    VerificationResult,
)
from witty_browser_auto.runtime.repair import ToolFailureKind
from witty_browser_auto.toolkit import (
    observation_to_dict,
    observation_to_prompt,
    tool_result_to_dict,
)


def _candidate(
    target_id: str,
    *,
    role: str = "button",
    name: str = "提交",
    text: str = "立即提交",
    confidence: float = 0.9,
    disabled: bool = False,
) -> CandidateTarget:
    return CandidateTarget(
        target_id,
        role,
        name,
        text,
        confidence,
        ("测试候选",),
        LocatorRecipe("css", value=f"#{target_id}"),
        BoundingBox(1.0, 2.0, 30.0, 40.0),
        disabled=disabled,
        drag_risk=DragRiskClass.BUSINESS,
    )


def _observation(
    *,
    candidates: tuple[CandidateTarget, ...] = (),
    summary: str = "页面标题：订单列表",
) -> Observation:
    return Observation(
        surface_id="surface-1",
        url="https://shop.test/orders?token=secret123",
        title="订单列表",
        version=7,
        fingerprint="fp-abc",
        summary=summary,
        candidates=candidates,
    )


# ----------------------------------------------------------------------
# 观察
# ----------------------------------------------------------------------


def test_observation_dict_is_json_serializable() -> None:
    observation = _observation(candidates=(_candidate("t-1"),))
    payload = observation_to_dict(observation)

    # datetime 与枚举都必须已经转成基础类型，否则 harness 塞不进模型上下文。
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "fp-abc" in encoded
    assert payload["captured_at"].endswith("+00:00")
    assert payload["observation_version"] == 7
    assert payload["candidates"][0]["drag_risk"] == "business"
    assert payload["candidates"][0]["target_id"] == "t-1"
    # 几何默认不带：模型选目标用不到像素，带上只占上下文。
    assert "box" not in payload["candidates"][0]
    assert observation_to_dict(observation, include_boxes=True)["candidates"][0]["box"] == {
        "x": 1.0,
        "y": 2.0,
        "width": 30.0,
        "height": 40.0,
    }


def test_candidates_are_capped_by_confidence_and_truncation_is_flagged() -> None:
    candidates = tuple(_candidate(f"t-{index}", confidence=index / 100) for index in range(1, 41))
    payload = observation_to_dict(_observation(candidates=candidates), max_candidates=5)

    assert payload["candidate_count"] == 40
    assert payload["returned_candidate_count"] == 5
    assert payload["candidates_truncated"] is True
    # 置信度最高的先留下，截断才不会把真正的目标丢掉。
    assert [item["target_id"] for item in payload["candidates"]] == [
        "t-40",
        "t-39",
        "t-38",
        "t-37",
        "t-36",
    ]


def test_untruncated_observation_carries_no_truncation_flag() -> None:
    payload = observation_to_dict(_observation(candidates=(_candidate("t-1"),)))
    assert "candidates_truncated" not in payload
    assert "summary_truncated" not in payload


def test_summary_and_text_are_clipped_with_explicit_flags() -> None:
    observation = _observation(
        candidates=(_candidate("t-1", text="很长的文本" * 100),),
        summary="摘要" * 2000,
    )
    payload = observation_to_dict(observation, max_summary_chars=50, max_text_chars=10)

    assert len(payload["summary"]) == 50
    assert payload["summary_truncated"] is True
    assert len(payload["candidates"][0]["text"]) == 10
    assert payload["candidates"][0]["text_truncated"] is True


def test_role_filter_selects_only_wanted_candidates() -> None:
    candidates = (
        _candidate("t-1", role="button"),
        _candidate("t-2", role="textbox"),
        _candidate("t-3", role="link"),
    )
    payload = observation_to_dict(_observation(candidates=candidates), roles=("textbox",))

    assert [item["target_id"] for item in payload["candidates"]] == ["t-2"]
    # 总数仍是页面真实候选数，避免调用方误判页面只有一个候选。
    assert payload["candidate_count"] == 3


def test_prompt_text_lists_target_ids_verbatim() -> None:
    observation = _observation(
        candidates=(_candidate("t-1"), _candidate("t-2", role="textbox", name="关键词"))
    )
    text = observation_to_prompt(observation)

    assert "订单列表" in text
    assert "t-1" in text and "t-2" in text
    assert "[button]" in text and "[textbox]" in text
    assert "页面摘要：" in text


def test_prompt_text_flags_disabled_candidates_and_scope() -> None:
    candidates = tuple(_candidate(f"t-{index}", confidence=0.5) for index in range(6))
    candidates += (_candidate("t-off", confidence=0.99, disabled=True),)
    text = observation_to_prompt(_observation(candidates=candidates), max_candidates=2)

    assert "2/7" in text
    # 禁用的候选点不动，置信度再高也排在最后，截断时先被挤掉。
    assert "t-off" not in text
    assert "已禁用" in observation_to_prompt(_observation(candidates=candidates))


def test_candidates_rank_inputs_and_controls_before_links() -> None:
    """两百个导航链接不能把搜索框挤出模型视野：可输入控件最先，链接最后。"""

    links = tuple(_candidate(f"l-{index}", role="link", confidence=0.95) for index in range(30))
    controls = (
        _candidate("search", role="textbox", name="搜索", confidence=0.68),
        _candidate("go", role="button", name="搜索", confidence=0.95),
        _candidate("agree", role="checkbox", name="同意", confidence=0.72),
    )
    observation = _observation(candidates=links + controls)

    payload = observation_to_dict(observation, max_candidates=5)
    ids = [item["target_id"] for item in payload["candidates"]]
    assert ids[0] == "search"
    assert ids[1:3] == ["go", "agree"]
    assert ids[3:] == ["l-0", "l-1"]


def test_candidates_in_viewport_rank_before_those_below_the_fold() -> None:
    above = CandidateTarget(
        "above",
        "link",
        "首页",
        "",
        0.9,
        ("测试",),
        LocatorRecipe("css", value="#above"),
        BoundingBox(0.0, 100.0, 50.0, 20.0),
    )
    below = CandidateTarget(
        "below",
        "link",
        "更多",
        "",
        0.95,
        ("测试",),
        LocatorRecipe("css", value="#below"),
        BoundingBox(0.0, 2400.0, 50.0, 20.0),
    )
    observation = Observation(
        surface_id="s",
        url="https://shop.test/",
        title="首页",
        version=1,
        fingerprint="fp",
        summary="",
        candidates=(below, above),
        metadata={"CSS视口": {"width": 1280, "height": 800}},
    )

    payload = observation_to_dict(observation)
    assert [item["target_id"] for item in payload["candidates"]] == ["above", "below"]

    # 不知道视口时不惩罚，退回置信度。
    unknown = Observation(
        surface_id="s",
        url="https://shop.test/",
        title="首页",
        version=1,
        fingerprint="fp",
        summary="",
        candidates=(below, above),
    )
    assert [item["target_id"] for item in observation_to_dict(unknown)["candidates"]] == [
        "below",
        "above",
    ]


def test_prompt_text_points_to_locators_when_no_candidates() -> None:
    text = observation_to_prompt(_observation())
    # 无候选时必须告诉模型改走显式定位器，否则它只会反复尝试不存在的 target_id。
    assert "显式定位器" in text


# ----------------------------------------------------------------------
# 工具结果
# ----------------------------------------------------------------------


def _result(**overrides: object) -> ToolExecutionResult:
    defaults: dict[str, object] = {
        "call_id": "call-1",
        "name": "click",
        "success": True,
        "message": "点击成功",
        "data": {"detail": "完整调用方数据", "token": "secret"},
    }
    defaults.update(overrides)
    return ToolExecutionResult(**defaults)  # type: ignore[arg-type]


def test_result_dict_prefers_the_bounded_model_view() -> None:
    result = _result(model_data={"detail": "有界视图"})
    model = tool_result_to_dict(result)
    caller = tool_result_to_dict(result, for_model=False)

    assert model["data"] == {"detail": "有界视图"}
    assert "data_is_caller_view" not in model
    assert caller["data"]["token"] == "secret"


def test_result_dict_marks_fallback_to_the_caller_view() -> None:
    # 工具没声明模型视图时会回退到完整数据，这件事必须让调用方看见。
    model = tool_result_to_dict(_result())
    assert model["data"]["token"] == "secret"
    assert model["data_is_caller_view"] is True


def test_result_dict_keeps_failure_kind_and_verification() -> None:
    result = _result(
        success=False,
        message="后置条件未满足",
        failure_kind=ToolFailureKind.VERIFICATION,
        verification=VerificationResult(False, "URL 未变化"),
    )
    payload = tool_result_to_dict(result)

    # 这两项决定下一步是重试、换路还是停下，缺了模型只能瞎猜。
    assert payload["failure_kind"] == "verification"
    assert payload["verification"] == {"success": False, "reason": "URL 未变化"}
    assert json.dumps(payload, ensure_ascii=False)


def test_result_dict_hides_evidence_paths_from_the_model() -> None:
    result = _result(
        evidence=EvidenceRef("ev-1", "screenshot", "/private/task/shot.png", "登录后首页"),
        receipt=ActionReceipt("act-1", True, True, "已派发", 12.5),
    )
    model = tool_result_to_dict(result)
    caller = tool_result_to_dict(result, for_model=False)

    assert model["evidence"] == {"kind": "screenshot", "summary": "登录后首页"}
    assert "/private/task/shot.png" not in json.dumps(model, ensure_ascii=False)
    assert caller["evidence"]["path"] == "/private/task/shot.png"
    # 动作回执属于调用方诊断信息，不进模型上下文。
    assert "receipt" not in model
    assert caller["receipt"]["duration_ms"] == 12.5


def test_result_dict_carries_the_post_action_page_snapshot() -> None:
    """动作后的新观察随结果给出，模型直接拿 page.candidates 里的 target_id 走下一步。"""

    observation = _observation(
        candidates=tuple(
            _candidate(f"t-{index}", confidence=1 - index / 100) for index in range(30)
        )
    )
    result = _result(observation=observation)

    payload = tool_result_to_dict(result)
    assert payload["page"]["url"] == observation.url
    assert payload["page"]["candidate_count"] == 30
    assert payload["page"]["returned_candidate_count"] == 24
    assert payload["page"]["candidates_truncated"] is True
    assert payload["page"]["candidates"][0]["target_id"] == "t-0"
    assert json.dumps(payload, ensure_ascii=False)

    trimmed = tool_result_to_dict(result, page_max_candidates=5, page_roles=("button",))
    assert trimmed["page"]["returned_candidate_count"] == 5

    assert "page" not in tool_result_to_dict(result, include_page=False)
    assert "page" not in tool_result_to_dict(_result())

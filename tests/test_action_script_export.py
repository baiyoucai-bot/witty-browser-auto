"""动作脚本导出的单元测试。"""

from __future__ import annotations

import json

import pytest

from witty_browser_auto.agent.script_tools import execute_script_tool
from witty_browser_auto.domain.models import (
    CandidateTarget,
    ExecutionScope,
    LocatorRecipe,
    Observation,
    TaskSpec,
)
from witty_browser_auto.toolkit.script_export import (
    ActionScriptLog,
    build_action_script,
    derive_locator,
)


def _candidate(
    target_id: str,
    *,
    role: str = "button",
    name: str = "登录",
    text: str = "登录",
    tag: str = "button",
    attrs: dict[str, str] | None = None,
    selector: str | None = None,
) -> CandidateTarget:
    descriptor: dict[str, object] = {
        "attrs": attrs or {},
        "name": name,
        "role": role,
        "tag": tag,
        "text": text,
    }
    if selector is not None:
        descriptor["selector"] = selector
    return CandidateTarget(
        target_id=target_id,
        role=role,
        name=name,
        text=text,
        confidence=0.9,
        reasons=("测试候选",),
        recipe=LocatorRecipe(
            strategy="ax_backend_node",
            role=role,
            name=name,
            value=json.dumps(descriptor, ensure_ascii=False, sort_keys=True),
            backend_node_id=42,
        ),
    )


def _observation(*candidates: CandidateTarget) -> Observation:
    return Observation(
        surface_id="surface-1",
        url="https://shop.test/login",
        title="登录",
        version=3,
        fingerprint="fp-abc",
        summary="登录页",
        candidates=candidates,
    )


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="t-1",
        goal="登录并下单",
        start_url="https://shop.test/login",
        scope=ExecutionScope(project_id="p", allowed_origins=("https://shop.test",)),
    )


# ----------------------------------------------------------------------
# 定位器反推
# ----------------------------------------------------------------------


def test_test_id_wins_over_everything_else() -> None:
    candidate = _candidate("n-1", attrs={"data-testid": "login-btn", "id": "submit"})
    locator, reason = derive_locator(candidate)
    assert locator == {"strategy": "test_id", "value": "login-btn"}
    assert reason == ""


def test_css_id_is_used_when_test_id_missing() -> None:
    locator, _ = derive_locator(_candidate("n-1", attrs={"id": "submit"}))
    assert locator == {"strategy": "css", "value": "#submit"}


def test_unsafe_css_id_is_skipped() -> None:
    # 含冒号或空格的 id 直接拼进选择器会变成非法 CSS，必须退到下一档策略。
    locator, _ = derive_locator(_candidate("n-1", attrs={"id": "a:b c"}))
    assert locator == {"strategy": "role", "value": "button", "name": "登录"}


def test_pointer_selector_is_used_before_role() -> None:
    locator, _ = derive_locator(_candidate("n-1", selector="nav > a.item"))
    assert locator == {"strategy": "css", "value": "nav > a.item"}


def test_role_and_name_used_when_no_attributes() -> None:
    locator, _ = derive_locator(_candidate("n-1"))
    assert locator == {"strategy": "role", "value": "button", "name": "登录"}


def test_name_attribute_falls_back_to_css() -> None:
    candidate = _candidate("n-1", role="", name="", text="", tag="input", attrs={"name": "user"})
    locator, _ = derive_locator(candidate)
    assert locator == {"strategy": "css", "value": 'input[name="user"]'}


def test_text_is_the_last_resort() -> None:
    candidate = _candidate("n-1", role="", name="", text="立即购买", tag="div")
    locator, _ = derive_locator(candidate)
    assert locator == {"strategy": "text", "value": "立即购买"}


def test_candidate_without_any_anchor_reports_reason() -> None:
    candidate = _candidate("n-1", role="", name="", text="", tag="")
    locator, reason = derive_locator(candidate)
    assert locator is None
    assert "手工补定位器" in reason


# ----------------------------------------------------------------------
# 录制规则
# ----------------------------------------------------------------------


def test_only_successful_page_actions_are_recorded() -> None:
    log = ActionScriptLog()
    observation = _observation(_candidate("n-1", attrs={"data-testid": "go"}))
    log.record(
        tool="click",
        arguments={"target_id": "n-1"},
        observation=observation,
        success=True,
        counts_as_action=True,
    )
    log.record(
        tool="click",
        arguments={"target_id": "n-1"},
        observation=observation,
        success=False,
        counts_as_action=True,
    )
    log.record(
        tool="inspect_network_traffic",
        arguments={},
        observation=observation,
        success=True,
        counts_as_action=False,
    )
    assert [step.tool for step in log.steps] == ["click_locator"]


def test_target_id_is_replaced_by_a_durable_locator() -> None:
    log = ActionScriptLog()
    log.record(
        tool="click",
        arguments={
            "target_id": "n-1",
            "expect_kind": "url_contains",
            "expect_value": "/home",
            "observation_fingerprint": "fp-abc",
        },
        observation=_observation(_candidate("n-1", attrs={"data-testid": "go"})),
        success=True,
        counts_as_action=True,
    )
    step = log.steps[0]
    assert step.tool == "click_locator"
    assert "target_id" not in step.arguments
    # 观察指纹是执行期状态，重跑时由门面重新绑定。
    assert "observation_fingerprint" not in step.arguments
    assert step.arguments["locator"] == {"strategy": "test_id", "value": "go"}
    assert step.arguments["expect_value"] == "/home"
    assert "登录" in step.description


def test_unknown_target_keeps_id_and_flags_manual_review() -> None:
    log = ActionScriptLog()
    log.record(
        tool="click",
        arguments={"target_id": "missing", "expect_kind": "url_contains", "expect_value": "/x"},
        observation=_observation(_candidate("n-1")),
        success=True,
        counts_as_action=True,
    )
    step = log.steps[0]
    assert step.tool == "click"
    assert step.arguments["target_id"] == "missing"
    assert any("跨会话失效" in note for note in step.notes)


def test_fingerprint_condition_drops_its_execution_time_value() -> None:
    log = ActionScriptLog()
    log.record(
        tool="click",
        arguments={
            "target_id": "n-1",
            "expect_kind": "fingerprint_changed",
            "expect_value": "fp-abc",
        },
        observation=_observation(_candidate("n-1", attrs={"data-testid": "go"})),
        success=True,
        counts_as_action=True,
    )
    step = log.steps[0]
    assert step.arguments["expect_kind"] == "fingerprint_changed"
    assert "expect_value" not in step.arguments


def test_target_exists_condition_is_downgraded_with_a_note() -> None:
    log = ActionScriptLog()
    log.record(
        tool="click",
        arguments={
            "target_id": "n-1",
            "expect_kind": "target_exists",
            "expect_value": "n-unknown",
        },
        observation=_observation(_candidate("n-1", attrs={"data-testid": "go"})),
        success=True,
        counts_as_action=True,
    )
    step = log.steps[0]
    assert step.arguments["expect_kind"] == "fingerprint_changed"
    assert "expect_value" not in step.arguments
    assert any("降级" in note for note in step.notes)


def test_input_keys_are_collected_across_steps() -> None:
    log = ActionScriptLog()
    observation = _observation(_candidate("n-1", attrs={"data-testid": "user"}))
    log.record(
        tool="input_text",
        arguments={"target_id": "n-1", "input_key": "username"},
        observation=observation,
        success=True,
        counts_as_action=True,
    )
    log.record(
        tool="upload_files",
        arguments={"path_input_keys": ["invoice", "receipt"]},
        observation=observation,
        success=True,
        counts_as_action=True,
    )
    assert log.referenced_input_keys() == ("username", "invoice", "receipt")
    assert log.steps[0].tool == "input_text_locator"


def test_nested_form_targets_are_replaced_with_stable_locators() -> None:
    log = ActionScriptLog()
    log.record(
        tool="fill_form",
        arguments={
            "fields": [
                {"target_id": "n-1", "input_key": "username"},
                {"target_id": "n-2", "text": "备注"},
            ]
        },
        observation=_observation(
            _candidate("n-1", attrs={"data-testid": "user"}),
            _candidate("n-2", attrs={"id": "note"}),
        ),
        success=True,
        counts_as_action=True,
    )

    fields = log.steps[0].arguments["fields"]
    assert all("target_id" not in field for field in fields)
    assert fields[0]["locator"] == {"strategy": "test_id", "value": "user"}
    assert fields[1]["locator"] == {"strategy": "css", "value": "#note"}


def test_drag_endpoints_are_replaced_with_stable_locators() -> None:
    log = ActionScriptLog()
    log.record(
        tool="drag_to_element",
        arguments={"source_target_id": "n-1", "target_target_id": "n-2"},
        observation=_observation(
            _candidate("n-1", attrs={"data-testid": "card"}),
            _candidate("n-2", attrs={"id": "bucket"}),
        ),
        success=True,
        counts_as_action=True,
    )

    step = log.steps[0]
    assert "source_target_id" not in step.arguments
    assert "target_target_id" not in step.arguments
    assert step.arguments["source_locator"] == {"strategy": "test_id", "value": "card"}
    assert step.arguments["target_locator"] == {"strategy": "css", "value": "#bucket"}


def test_header_input_key_mappings_contribute_their_values() -> None:
    # manage_network_route 的 *_header_input_keys 是「Header 名 → 输入键」的映射，
    # 输入键在取值一侧；按键名收集会漏掉它们，脚本重跑时才会在运行期报错。
    log = ActionScriptLog()
    log.record(
        tool="manage_network_route",
        arguments={
            "operation": "add",
            "request_header_input_keys": {"Authorization": "api_token"},
            "response_header_input_keys": {"X-Trace": "trace_id"},
        },
        observation=_observation(),
        success=True,
        counts_as_action=True,
    )
    assert log.referenced_input_keys() == ("api_token", "trace_id")


# ----------------------------------------------------------------------
# 脚本渲染
# ----------------------------------------------------------------------


def _filled_log() -> ActionScriptLog:
    log = ActionScriptLog()
    observation = _observation(
        _candidate("n-1", name="用户名", attrs={"data-testid": "user"}),
        _candidate("n-2", name="提交", attrs={"id": "submit"}),
    )
    log.record(
        tool="input_text",
        arguments={"target_id": "n-1", "input_key": "username"},
        observation=observation,
        success=True,
        counts_as_action=True,
    )
    log.record(
        tool="click",
        arguments={"target_id": "n-2", "expect_kind": "url_contains", "expect_value": "/home"},
        observation=observation,
        success=True,
        counts_as_action=True,
    )
    log.record(
        tool="scroll",
        arguments={"amount": 800},
        observation=observation,
        success=True,
        counts_as_action=True,
    )
    return log


def test_generated_script_is_valid_python() -> None:
    log = _filled_log()
    code = build_action_script(log.steps, task=_task(), input_keys=log.referenced_input_keys())
    compile(code, "<generated>", "exec")


def test_generated_script_carries_entry_and_inputs() -> None:
    log = _filled_log()
    code = build_action_script(log.steps, task=_task(), input_keys=log.referenced_input_keys())
    assert 'START_URL = "https://shop.test/login"' in code
    assert '"https://shop.test"' in code
    assert '"username": ""' in code
    assert "await toolkit.call(" in code
    assert '"input_text_locator"' in code
    assert '"click_locator"' in code
    assert "assert result.success" in code
    # 会话内标识不得进入脚本。
    assert "n-1" not in code
    assert "fp-abc" not in code


def test_empty_log_is_rejected() -> None:
    with pytest.raises(ValueError, match="还没有可导出"):
        build_action_script((), task=_task())


def test_unknown_target_is_rejected() -> None:
    log = _filled_log()
    with pytest.raises(ValueError, match="不支持的脚本目标"):
        build_action_script(log.steps, task=_task(), target="playwright")


# ----------------------------------------------------------------------
# 工具执行层
# ----------------------------------------------------------------------


def test_tool_hides_script_body_from_the_model() -> None:
    log = _filled_log()
    outcome = execute_script_tool({}, log=log, task=_task())
    assert "toolkit.call" in outcome.data["code"]
    assert outcome.data["step_count"] == 3
    assert outcome.data["input_keys"] == ["username"]
    assert "code" not in outcome.model_data
    assert outcome.model_data["tools"] == ["input_text_locator", "click_locator", "scroll"]
    assert outcome.model_data["needs_manual_review_count"] == 0


def test_tool_reports_steps_needing_manual_review() -> None:
    log = ActionScriptLog()
    log.record(
        tool="click",
        arguments={"target_id": "missing", "expect_kind": "url_contains", "expect_value": "/x"},
        observation=_observation(),
        success=True,
        counts_as_action=True,
    )
    outcome = execute_script_tool({}, log=log, task=_task())
    assert outcome.model_data["needs_manual_review_count"] == 1
    assert "人工复核" in outcome.message


def test_tool_rejects_unknown_arguments() -> None:
    with pytest.raises(ValueError, match="未知参数"):
        execute_script_tool({"format": "python"}, log=_filled_log(), task=_task())

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from witty_browser_auto.agent import (
    capability_tools,
    element_tools,
    locator_tools,
    network_tools,
    tab_tools,
    traffic_tools,
)
from witty_browser_auto.agent import page_diagnostics as diagnostics
from witty_browser_auto.agent.tool_schemas import TOOL_SCHEMAS
from witty_browser_auto.toolkit import (
    BROWSER_TOOLS,
    describe_tools,
    tool_schemas,
    validate_tool_arguments,
)
from witty_browser_auto.toolkit.catalog import (
    CAPABILITY_TOOLS,
    DIAGNOSTIC_TOOLS,
    ELEMENT_READ_TOOLS,
    LOCATOR_TOOLS,
    NETWORK_TOOLS,
    PAGE_CONTROL_TOOLS,
    TAB_TOOLS,
    names_of,
    schemas_of,
)
from witty_browser_auto.toolkit.registry import (
    ToolArgumentError,
    ToolContractError,
    ToolDefinition,
    ToolRegistry,
)

_BASELINE_PATH = Path(__file__).parent / "data" / "tool_schema_baseline.json"

# 这四个工具决定任务终态或等待，只能由智能体循环处理，外部调用方不得直接执行。
ENGINE_ONLY_TOOLS = frozenset({"wait_until", "finish", "ask_user", "block"})


def _baseline() -> list[dict[str, Any]]:
    return list(json.loads(_BASELINE_PATH.read_text(encoding="utf-8")))


def test_existing_tool_contracts_stay_backward_compatible() -> None:
    """已有工具的调用方式是既成事实。

    基线是首个冻结版本而不是上一版快照，所以每次改动都要对最初的契约保持兼容，而不是
    只对上一次改动兼容。新增可选参数是允许的；删除参数、新增必填项、改类型或收窄枚举
    都会让已经写好的外部调用直接失效。
    """

    # 基线覆盖全部已声明工具，含四个仅引擎可用的终态工具，因此这里要显式取全量。
    generated = {
        schema["function"]["name"]: schema for schema in tool_schemas(include_engine_only=True)
    }
    for expected in _baseline():
        name = expected["function"]["name"]
        assert name in generated, f"已有工具 {name} 从注册表中消失"
        _assert_compatible(name, expected["function"], generated[name]["function"])


def _assert_compatible(name: str, baseline: dict[str, Any], current: dict[str, Any]) -> None:
    baseline_params = baseline.get("parameters", {})
    current_params = current.get("parameters", {})
    baseline_properties = baseline_params.get("properties", {})
    current_properties = current_params.get("properties", {})

    missing = set(baseline_properties) - set(current_properties)
    assert not missing, f"工具 {name} 删除了已有参数：{'、'.join(sorted(missing))}"

    added_required = set(current_params.get("required", ())) - set(
        baseline_params.get("required", ())
    )
    assert not added_required, f"工具 {name} 新增了必填参数：{'、'.join(sorted(added_required))}"

    for key, baseline_schema in baseline_properties.items():
        current_schema = current_properties[key]
        assert current_schema.get("type") == baseline_schema.get("type"), (
            f"工具 {name} 的参数 {key} 改变了类型"
        )
        baseline_enum = baseline_schema.get("enum")
        if baseline_enum is not None:
            current_enum = current_schema.get("enum")
            assert current_enum is not None and not set(baseline_enum) - set(current_enum), (
                f"工具 {name} 的参数 {key} 收窄了枚举取值"
            )
        if baseline_schema.get("type") == "object":
            _assert_nested_object_compatible(f"{name}.{key}", baseline_schema, current_schema)


def _assert_nested_object_compatible(
    path: str,
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> None:
    """定位器等嵌套对象同样是对外契约，必填项与已有字段不能变。"""

    baseline_properties = baseline.get("properties")
    if not isinstance(baseline_properties, dict):
        return
    current_properties = current.get("properties", {})
    missing = set(baseline_properties) - set(current_properties)
    assert not missing, f"{path} 删除了已有字段：{'、'.join(sorted(missing))}"
    added_required = set(current.get("required", ())) - set(baseline.get("required", ()))
    assert not added_required, f"{path} 新增了必填字段：{'、'.join(sorted(added_required))}"
    for key, baseline_schema in baseline_properties.items():
        assert current_properties[key].get("type") == baseline_schema.get("type"), (
            f"{path}.{key} 改变了类型"
        )


def test_model_schemas_are_derived_from_registry() -> None:
    assert list(TOOL_SCHEMAS) == list(BROWSER_TOOLS.schemas())
    assert len(TOOL_SCHEMAS) == len(BROWSER_TOOLS)


def test_agent_schema_groups_stay_aligned_with_catalog() -> None:
    """执行层按名称集合分发，名称与 schema 必须来自同一份声明。"""

    assert network_tools.NETWORK_TOOL_SCHEMAS == schemas_of(NETWORK_TOOLS)
    assert network_tools.NETWORK_TOOL_NAMES == names_of(NETWORK_TOOLS)
    assert tab_tools.TAB_TOOL_SCHEMAS == schemas_of(TAB_TOOLS)
    assert tab_tools.TAB_TOOL_NAMES == names_of(TAB_TOOLS)
    assert locator_tools.LOCATOR_ACTION_SCHEMAS == schemas_of(LOCATOR_TOOLS)
    assert locator_tools.LOCATOR_ACTION_TOOL_NAMES == names_of(LOCATOR_TOOLS)
    assert diagnostics.PAGE_DIAGNOSTIC_TOOL_SCHEMAS == schemas_of(DIAGNOSTIC_TOOLS)
    assert diagnostics.PAGE_DIAGNOSTIC_TOOL_NAME == DIAGNOSTIC_TOOLS[0].name
    assert capability_tools.CAPABILITY_GAP_TOOL_SCHEMAS == schemas_of(CAPABILITY_TOOLS)
    assert capability_tools.CAPABILITY_GAP_TOOL_NAME == CAPABILITY_TOOLS[0].name
    assert element_tools.ELEMENT_READ_TOOL_SCHEMAS == schemas_of(ELEMENT_READ_TOOLS)
    assert element_tools.ELEMENT_READ_TOOL_NAMES == names_of(ELEMENT_READ_TOOLS)
    assert element_tools.PAGE_CONTROL_TOOL_SCHEMAS == schemas_of(PAGE_CONTROL_TOOLS)
    assert element_tools.PAGE_CONTROL_TOOL_NAMES == names_of(PAGE_CONTROL_TOOLS)


def test_registered_categories_match_execution_routes() -> None:
    assert {tool.name for tool in BROWSER_TOOLS.in_category("network")} == set(
        network_tools.NETWORK_TOOL_NAMES
    ) | set(traffic_tools.TRAFFIC_TOOL_NAMES)
    assert not set(network_tools.NETWORK_TOOL_NAMES) & set(traffic_tools.TRAFFIC_TOOL_NAMES)
    assert {tool.name for tool in BROWSER_TOOLS.in_category("tab")} == set(tab_tools.TAB_TOOL_NAMES)
    assert {tool.name for tool in BROWSER_TOOLS.in_category("diagnostics")} == {
        diagnostics.PAGE_DIAGNOSTIC_TOOL_NAME
    }
    assert {tool.name for tool in BROWSER_TOOLS.in_category("capability")} == {
        capability_tools.CAPABILITY_GAP_TOOL_NAME
    }


def test_engine_only_tools_are_closed_to_external_callers() -> None:
    closed = {tool.name for tool in BROWSER_TOOLS if not tool.externally_callable}
    assert closed == ENGINE_ONLY_TOOLS
    for name in ENGINE_ONLY_TOOLS:
        assert BROWSER_TOOLS.get(name).unavailable_reason.strip()


def test_generated_schemas_are_independent_copies() -> None:
    """下发前会按当前观察改写 schema，注册表本身不能被污染。"""

    first = BROWSER_TOOLS.get("click").json_schema()
    first["function"]["parameters"]["properties"]["target_id"]["enum"] = ["t-1"]
    second = BROWSER_TOOLS.get("click").json_schema()
    assert "enum" not in second["function"]["parameters"]["properties"]["target_id"]


def test_validate_rejects_unknown_parameter() -> None:
    with pytest.raises(ToolArgumentError, match="未知参数"):
        validate_tool_arguments("navigate", {"url": "https://example.com", "depth": 1})


def test_validate_rejects_missing_required_parameter() -> None:
    with pytest.raises(ToolArgumentError, match="缺少必填参数"):
        validate_tool_arguments("click", {"expect_kind": "url_contains", "expect_value": "/x"})
    # 等待的条件就是它的全部意义，不能像 click 那样缺省成"页面有变化"。
    with pytest.raises(ToolArgumentError, match="缺少必填参数"):
        validate_tool_arguments("wait_for_condition", {"expect_kind": "text_contains"})


def test_click_postcondition_is_optional_in_the_contract() -> None:
    """探索性点击说不清结果；契约层放行，门面在执行前补上页面变化判据。"""

    assert validate_tool_arguments("click", {"target_id": "t-1"}) == {"target_id": "t-1"}
    for name in ("click_locator", "select_locator"):
        assert "expect_kind" not in BROWSER_TOOLS.get(name).required
    for name in ("hover", "press_key", "navigate_history", "select"):
        assert "expect_kind" not in BROWSER_TOOLS.get(name).required


def test_validate_rejects_wrong_type() -> None:
    with pytest.raises(ToolArgumentError, match="必须是字符串"):
        validate_tool_arguments("navigate", {"url": 42})


def test_validate_rejects_boolean_as_number() -> None:
    """布尔值在 Python 里是整数子类，数值参数必须显式排除。"""

    with pytest.raises(ToolArgumentError, match="必须是数字"):
        validate_tool_arguments("wait", {"seconds": True})


def test_validate_enforces_numeric_bounds() -> None:
    with pytest.raises(ToolArgumentError, match="不能大于 10"):
        validate_tool_arguments("wait", {"seconds": 30})
    assert validate_tool_arguments("wait", {"seconds": 3}) == {"seconds": 3}


def test_validate_enforces_enum() -> None:
    with pytest.raises(ToolArgumentError, match="枚举值"):
        validate_tool_arguments("manage_network_route", {"operation": "purge"})


def test_validate_enforces_string_length() -> None:
    with pytest.raises(ToolArgumentError, match="最多允许 600 个字符"):
        validate_tool_arguments(
            "report_capability_gap",
            {"area": "locator", "capability": "x" * 601, "evidence": "证据"},
        )


def test_validate_enforces_pattern() -> None:
    with pytest.raises(ToolArgumentError, match="不符合声明的格式"):
        validate_tool_arguments(
            "ask_user",
            {
                "question": "请提供订单号",
                "reason": "missing_business_fact",
                "input_key": "9invalid",
                "answer_type": "identifier",
            },
        )


def test_validate_checks_nested_locator_object() -> None:
    with pytest.raises(ToolArgumentError, match="不接受未知字段"):
        validate_tool_arguments(
            "click_locator",
            {
                "locator": {"strategy": "css", "value": "#ok", "script": "alert(1)"},
                "expect_kind": "url_contains",
                "expect_value": "/done",
            },
        )
    with pytest.raises(ToolArgumentError, match="缺少必填字段"):
        validate_tool_arguments(
            "click_locator",
            {
                "locator": {"strategy": "css"},
                "expect_kind": "url_contains",
                "expect_value": "/done",
            },
        )


def test_validate_rejects_duplicate_array_items() -> None:
    with pytest.raises(ToolArgumentError, match="重复项"):
        validate_tool_arguments(
            "export_network_response",
            {"collection_name": "订单", "candidate_ids": ["a", "a"]},
        )


def test_validate_returns_normalized_arguments() -> None:
    validated = validate_tool_arguments(
        "click_locator",
        {
            "locator": {"strategy": "css", "value": "#submit"},
            "expect_kind": "url_contains",
            "expect_value": "/done",
        },
    )
    assert validated["locator"] == {"strategy": "css", "value": "#submit"}
    assert validated["expect_kind"] == "url_contains"


def test_describe_exposes_call_contract_for_other_agents() -> None:
    described = {item["name"]: item for item in describe_tools()}
    navigate = described["navigate"]
    assert navigate["category"] == "navigation"
    assert navigate["returns"]
    assert navigate["parameters"] == [
        {
            "name": "url",
            "type": "string",
            "required": True,
            "description": "",
            "constraints": {},
        }
    ]
    assert described["wait"]["parameters"][0]["constraints"] == {"minimum": 0, "maximum": 10}
    # 默认只给可外部调用的工具：把终态工具混进去只会诱导模型调用必被拒绝的动作。
    assert not ENGINE_ONLY_TOOLS & set(described)
    full = {item["name"]: item for item in describe_tools(include_engine_only=True)}
    assert full["finish"]["externally_callable"] is False
    assert full["finish"]["unavailable_reason"]


def test_default_schemas_exclude_engine_only_tools() -> None:
    """外部 harness 直接下发 tool_schemas()，终态工具混进去就是一次无效回合。"""

    default_names = {schema["function"]["name"] for schema in tool_schemas()}
    assert not ENGINE_ONLY_TOOLS & default_names
    assert len(default_names) == len(BROWSER_TOOLS.externally_callable())

    full_names = {schema["function"]["name"] for schema in tool_schemas(include_engine_only=True)}
    assert ENGINE_ONLY_TOOLS <= full_names
    assert len(full_names) == len(BROWSER_TOOLS)

    network_only = {schema["function"]["name"] for schema in tool_schemas(category="network")}
    assert "inspect_network_traffic" in network_only
    assert "click" not in network_only


def test_registry_rejects_duplicate_tool_names() -> None:
    definition = ToolDefinition(name="demo_tool", description="演示", category="page")
    registry = ToolRegistry((definition,))
    with pytest.raises(ToolContractError, match="重复注册"):
        registry.register(definition)


def test_registry_rejects_unknown_tool_lookup() -> None:
    with pytest.raises(ToolArgumentError, match="未注册的工具"):
        BROWSER_TOOLS.get("open_devtools")


def test_definition_requires_reason_when_closed_to_external_callers() -> None:
    with pytest.raises(ToolContractError, match="必须说明原因"):
        ToolDefinition(
            name="demo_tool",
            description="演示",
            category="page",
            externally_callable=False,
        )


def test_definition_rejects_required_outside_properties() -> None:
    with pytest.raises(ToolContractError, match="未在属性中声明"):
        ToolDefinition(
            name="demo_tool",
            description="演示",
            category="page",
            properties={"a": {"type": "string"}},
            required=("b",),
        )


def test_definition_rejects_unknown_category() -> None:
    with pytest.raises(ToolContractError, match="分类不在允许范围"):
        ToolDefinition(name="demo_tool", description="演示", category="magic")

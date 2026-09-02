"""接口契约剖析与代码导出的单元测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from witty_browser_auto.config import NetworkTrafficConfig
from witty_browser_auto.network.api_analysis import (
    analyze_endpoint,
    classify_param,
    find_record_path,
    infer_schema,
    model_view,
    path_template,
)
from witty_browser_auto.network.codegen import CodeGenerationError, build_request_code
from witty_browser_auto.network.inspection import NetworkTrafficInspector
from witty_browser_auto.network.traffic import NetworkBody, NetworkExchange, NetworkTrafficLog


def _exchange(
    exchange_id: str,
    *,
    url: str,
    method: str = "GET",
    status: int | None = 200,
    request_headers: dict[str, str] | None = None,
    request_body: str | None = None,
    response_body: str | None = None,
    started: float = 1000.0,
) -> NetworkExchange:
    exchange = NetworkExchange(
        exchange_id=exchange_id,
        request_id=exchange_id,
        session_id="session-1",
        method=method,
        url=url,
        status=status,
        started_wall=started,
        state="finished",
    )
    exchange.request_headers = request_headers or {}
    if request_body is not None:
        exchange.request_body = NetworkBody(
            text=request_body, byte_length=len(request_body.encode("utf-8"))
        )
    if response_body is not None:
        exchange.response_body = NetworkBody(
            text=response_body, byte_length=len(response_body.encode("utf-8"))
        )
        exchange.mime_type = "application/json"
        exchange.response_headers = {"Content-Type": "application/json"}
    return exchange


# ----------------------------------------------------------------------
# URL 模板与参数
# ----------------------------------------------------------------------


def test_path_template_parameterizes_identifier_segments() -> None:
    assert path_template("/api/orders/12345/items") == "/api/orders/{id}/items"
    assert path_template("/v1/user/550e8400-e29b-41d4-a716-446655440000") == "/v1/user/{uuid}"
    assert path_template("/files/a1b2c3d4e5f60718293a") == "/files/{hash}"
    assert path_template("/api/orders/list") == "/api/orders/list"


def test_classify_param_separates_pagination_from_credentials() -> None:
    assert classify_param("pageSize") == "pagination"
    assert classify_param("cursor") == "pagination"
    assert classify_param("orderBy") == "sort"
    assert classify_param("access_token") == "credential"
    assert classify_param("sign") == "credential"
    assert classify_param("keyword") == "filter"


def test_query_params_report_variation_across_samples() -> None:
    exchanges = [
        _exchange("ex-1", url="https://shop.test/api/orders?page=1&size=20&status=paid"),
        _exchange("ex-2", url="https://shop.test/api/orders?page=2&size=20&status=paid"),
    ]
    analysis = analyze_endpoint(exchanges)
    params = {item["name"]: item for item in analysis["query_params"]}
    assert params["page"]["role"] == "pagination"
    assert params["page"]["varies"] is True
    assert params["size"]["varies"] is False
    assert params["status"]["always_present"] is True
    assert analysis["pagination"]["strategy"] == "page_number"


# ----------------------------------------------------------------------
# Schema 推断
# ----------------------------------------------------------------------


def test_infer_schema_describes_nested_structures() -> None:
    schema = infer_schema({"id": 1, "name": "甲", "tags": ["a"], "meta": {"ok": True}})
    assert schema["type"] == "object"
    assert schema["fields"]["id"]["type"] == "integer"
    assert schema["fields"]["name"]["type"] == "string"
    assert schema["fields"]["tags"]["type"] == "array"
    assert schema["fields"]["tags"]["item"]["type"] == "string"
    assert schema["fields"]["meta"]["fields"]["ok"]["type"] == "boolean"


def test_find_record_path_locates_largest_object_array() -> None:
    payload = {"code": 0, "data": {"list": [{"id": 1}, {"id": 2}], "banner": [{"x": 1}]}}
    path, count = find_record_path(payload)
    assert path == ["data", "list"]
    assert count == 2


def test_analysis_reports_record_path_and_total_fields() -> None:
    body = json.dumps({"total": 87, "hasMore": True, "data": [{"id": 1, "amount": 12}]})
    exchange = _exchange(
        "ex-1",
        url="https://shop.test/api/orders?page=1",
        response_body=body,
    )
    analysis = analyze_endpoint([exchange])
    assert analysis["response"]["record_path"] == ["data"]
    assert analysis["response"]["record_fields"] == ["amount", "id"]
    assert analysis["response"]["total_fields"] == ["total"]
    assert "hasMore" in analysis["response"]["pagination_fields"]


# ----------------------------------------------------------------------
# 鉴权与请求体
# ----------------------------------------------------------------------


def test_auth_detection_records_location_not_credential() -> None:
    exchange = _exchange(
        "ex-1",
        url="https://shop.test/api/orders?access_token=abc123",
        request_headers={
            "Authorization": "Bearer eyJhbGciOi.secret.value",
            "Cookie": "sid=s3cr3t; theme=dark",
            "X-Api-Key": "k-123456",
        },
    )
    analysis = analyze_endpoint([exchange])
    auth = analysis["auth"]
    assert auth["authorization_schemes"] == ["Bearer"]
    assert auth["cookie_names"] == ["sid", "theme"]
    assert "X-Api-Key" in auth["credential_headers"]
    assert auth["credential_query_params"] == ["access_token"]
    assert auth["requires_cookies"] is True
    # 凭据本身不得出现在契约结论里。
    assert "s3cr3t" not in json.dumps(auth, ensure_ascii=False)
    assert "eyJhbGciOi" not in json.dumps(auth, ensure_ascii=False)


def test_graphql_request_body_is_recognized() -> None:
    body = json.dumps(
        {
            "operationName": "Orders",
            "query": "query Orders($page: Int) { orders(page: $page) { id } }",
            "variables": {"page": 1},
        }
    )
    exchange = _exchange(
        "ex-1",
        url="https://shop.test/graphql",
        method="POST",
        request_headers={"Content-Type": "application/json"},
        request_body=body,
    )
    analysis = analyze_endpoint([exchange])
    graphql = analysis["request_body"]["graphql"]
    assert graphql["operation_name"] == "Orders"
    assert graphql["variable_names"] == ["page"]
    assert "query Orders" in graphql["query_preview"]


def test_form_request_body_lists_fields() -> None:
    exchange = _exchange(
        "ex-1",
        url="https://shop.test/api/login",
        method="POST",
        request_headers={"Content-Type": "application/x-www-form-urlencoded"},
        request_body="username=alice&remember=true&page=2",
    )
    analysis = analyze_endpoint([exchange])
    body = analysis["request_body"]
    assert body["format"] == "form"
    names = {item["name"]: item["value_type"] for item in body["fields"]}
    assert names == {"username": "string", "remember": "boolean", "page": "integer"}


# ----------------------------------------------------------------------
# 模型视图必须去掉业务取值
# ----------------------------------------------------------------------


def test_model_view_strips_response_samples_and_header_values() -> None:
    body = json.dumps({"data": [{"id": 1, "buyer": "张三", "phone": "13800000000"}]})
    exchange = _exchange(
        "ex-1",
        url="https://shop.test/api/orders?access_token=abc123",
        request_headers={"Authorization": "Bearer secret-token", "Cookie": "sid=s3cr3t"},
        response_body=body,
    )
    analysis = analyze_endpoint([exchange])
    # 调用方视图保留取值，便于直接写代码。
    assert "张三" in json.dumps(analysis["response"]["schema"], ensure_ascii=False)

    model = model_view(analysis)
    serialized = json.dumps(model, ensure_ascii=False)
    assert "张三" not in serialized
    assert "13800000000" not in serialized
    assert "secret-token" not in serialized
    assert "s3cr3t" not in serialized
    # 结构本身必须保留，否则模型写不出代码。
    assert model["response"]["schema"]["fields"]["data"]["item"]["fields"]["buyer"]["type"] == (
        "string"
    )
    assert model["request_header_names"] == ["Authorization", "Cookie"]
    credential = next(item for item in model["query_params"] if item["name"] == "access_token")
    assert credential["samples"] == ["***已脱敏***"]


# ----------------------------------------------------------------------
# 代码导出
# ----------------------------------------------------------------------


def _code(target: str, **overrides: object) -> dict:
    payload = {
        "target": target,
        "method": "POST",
        "url": "https://shop.test/api/orders",
        "headers": {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "Bearer secret-token",
            "Cookie": "sid=s3cr3t",
            "Content-Length": "13",
            ":authority": "shop.test",
        },
        "body": '{"page": 1}',
    }
    payload.update(overrides)
    return build_request_code(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "target",
    ["curl", "python_requests", "python_httpx", "javascript_fetch", "node_axios"],
)
def test_generated_code_never_leaks_credentials_by_default(target: str) -> None:
    result = _code(target)
    assert "secret-token" not in result["code"]
    assert "s3cr3t" not in result["code"]
    envs = {item["env"] for item in result["placeholders"]}
    assert envs == {"AUTHORIZATION", "COOKIE"}


@pytest.mark.parametrize(
    "target",
    ["curl", "python_requests", "python_httpx", "javascript_fetch", "node_axios"],
)
def test_generated_code_drops_browser_managed_headers(target: str) -> None:
    code = _code(target)["code"]
    assert "Content-Length" not in code
    assert ":authority" not in code
    assert "Accept" in code


def test_include_secrets_inlines_real_values() -> None:
    result = _code("curl", include_secrets=True)
    assert "Bearer secret-token" in result["code"]
    assert result["placeholders"] == []


def test_python_requests_uses_json_keyword_for_json_body() -> None:
    result = _code("python_requests")
    assert "json=payload" in result["code"]
    assert result["body_kind"] == "json"
    assert 'os.environ["AUTHORIZATION"]' in result["code"]


def test_python_requests_uses_data_keyword_for_form_body() -> None:
    result = _code(
        "python_requests",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body="a=1&b=2",
    )
    assert "data=payload" in result["code"]
    assert result["body_kind"] == "form"


def test_generated_python_code_is_syntactically_valid() -> None:
    for target in ("python_requests", "python_httpx"):
        compile(_code(target)["code"], f"<{target}>", "exec")


def test_curl_escapes_single_quotes_in_values() -> None:
    result = _code("curl", headers={"X-Note": "it's fine"}, body=None)
    assert "'\\''" in result["code"]


def test_binary_body_is_omitted_instead_of_inlined() -> None:
    result = _code("curl", body="AAAA", body_is_binary=True)
    assert result["body_omitted_binary"] is True
    assert "--data-raw" not in result["code"]


def test_unknown_target_is_rejected() -> None:
    with pytest.raises(CodeGenerationError, match="不支持的代码目标"):
        _code("ruby_faraday")


# ----------------------------------------------------------------------
# 门面层：inspector 分支
# ----------------------------------------------------------------------


def _inspector(tmp_path: Path) -> tuple[NetworkTrafficInspector, NetworkTrafficLog]:
    config = NetworkTrafficConfig()
    log = NetworkTrafficLog(config)
    inspector = NetworkTrafficInspector(log, tmp_path, config=config)
    return inspector, log


def _register(log: NetworkTrafficLog, exchange: NetworkExchange) -> None:
    log._exchanges[exchange.exchange_id] = exchange


def test_inspector_groups_same_endpoint_across_exchanges(tmp_path: Path) -> None:
    inspector, log = _inspector(tmp_path)
    _register(log, _exchange("ex-1", url="https://shop.test/api/orders/1", started=1.0))
    _register(log, _exchange("ex-2", url="https://shop.test/api/orders/2", started=2.0))
    _register(log, _exchange("ex-3", url="https://shop.test/api/users/9", started=3.0))

    full, model = asyncio.run(inspector.analyze_api({"exchange_id": "ex-1"}))
    assert full["endpoint"]["path_template"] == "/api/orders/{id}"
    assert full["sample_count"] == 2
    assert set(full["related_exchange_ids"]) == {"ex-1", "ex-2"}
    assert model["endpoint"]["url_template"] == "https://shop.test/api/orders/{id}"


def test_inspector_analyze_by_url_prefers_successful_exchange(tmp_path: Path) -> None:
    inspector, log = _inspector(tmp_path)
    _register(log, _exchange("ex-1", url="https://shop.test/api/orders", status=500, started=1.0))
    _register(log, _exchange("ex-2", url="https://shop.test/api/orders", status=200, started=2.0))

    full, _ = asyncio.run(inspector.analyze_api({"url_contains": "/api/orders"}))
    assert full["sample_exchange_id"] == "ex-2"


def test_inspector_requires_a_selector(tmp_path: Path) -> None:
    inspector, _ = _inspector(tmp_path)
    with pytest.raises(ValueError, match="exchange_id 或 url_contains"):
        asyncio.run(inspector.analyze_api({}))


def test_inspector_export_code_hides_code_from_model_view(tmp_path: Path) -> None:
    inspector, log = _inspector(tmp_path)
    _register(
        log,
        _exchange(
            "ex-1",
            url="https://shop.test/api/orders",
            method="POST",
            request_headers={"Authorization": "Bearer secret-token"},
            request_body='{"page": 1}',
        ),
    )
    full, model = asyncio.run(
        inspector.export_code({"exchange_id": "ex-1", "target": "python_requests"})
    )
    assert "requests.request" in full["code"]
    assert "code" not in model
    assert model["placeholder_env_names"] == ["AUTHORIZATION"]
    assert "secret-token" not in json.dumps(model, ensure_ascii=False)

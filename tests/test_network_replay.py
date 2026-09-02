"""请求重放的参数合并、受限 Header 分流与一次性拦截行为。"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from witty_browser_auto.network.replay import (
    ReplayRequest,
    _script_safe_headers,
    build_replay_request,
    perform_replay,
)
from witty_browser_auto.network.routing import ReplayInterception

ALLOWED = ("https://example.com",)
SOURCE = {
    "exchange_id": "ex-000007",
    "url": "https://example.com/api/orders?page=1",
    "method": "POST",
    "request_headers": {
        "Accept": "application/json",
        "Cookie": "session=abc",
        "Content-Length": "18",
        "If-None-Match": 'W/"v1"',
    },
    "request_body_text": '{"page":1}',
}


class FakeReplaySession:
    """按固定模板返回重放结果的假页面会话。"""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload
        self.expressions: list[str] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert method == "Runtime.evaluate"
        assert params is not None
        assert params["awaitPromise"] is True
        self.expressions.append(params["expression"])
        if self.payload is None:
            return {"exceptionDetails": {"text": "TypeError: failed to fetch"}}
        return {"result": {"value": json.dumps(self.payload)}}

    @property
    def spec(self) -> dict[str, Any]:
        """从模板调用表达式里取回传给页面的结构化参数。"""

        expression = self.expressions[-1]
        start = expression.rindex("})(") + 3
        return json.loads(expression[start:-1])


class FakeRouter:
    def __init__(self) -> None:
        self.armed: list[ReplayInterception] = []
        self.disarm_count = 0

    async def arm_replay(self, interception: ReplayInterception) -> None:
        self.armed.append(interception)

    async def disarm_replay(self) -> None:
        self.disarm_count += 1


def _ok_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "ok": True,
        "status": 200,
        "status_text": "OK",
        "headers": [["content-type", "application/json"], ["x-trace", "t-1"]],
        "body_base64": base64.b64encode(b'{"total":2}').decode("ascii"),
        "byte_length": len(b'{"total":2}'),
        "duration_ms": 31,
        "response_type": "basic",
        "final_url": "https://example.com/api/orders?page=1",
    }
    payload.update(overrides)
    return payload


def test_replay_inherits_source_request_when_no_override_is_given() -> None:
    request = build_replay_request({}, source=SOURCE, allowed_origins=ALLOWED)
    assert request.url == "https://example.com/api/orders?page=1"
    assert request.method == "POST"
    assert request.body == '{"page":1}'
    assert dict(request.headers)["Cookie"] == "session=abc"
    assert request.source_exchange_id == "ex-000007"


def test_browser_managed_headers_are_dropped_from_the_replay() -> None:
    request = build_replay_request({}, source=SOURCE, allowed_origins=ALLOWED)
    assert "Content-Length" not in dict(request.headers)


def test_overrides_replace_source_fields_and_remove_headers() -> None:
    request = build_replay_request(
        {
            "method": "get",
            "headers": {"Cookie": "session=other", "X-Trace": "probe"},
            "remove_headers": ["If-None-Match"],
        },
        source=SOURCE,
        allowed_origins=ALLOWED,
    )
    headers = dict(request.headers)
    assert request.method == "GET"
    assert headers["Cookie"] == "session=other"
    assert headers["X-Trace"] == "probe"
    assert "If-None-Match" not in headers
    assert request.body is None, "GET 不应该携带来源请求体"


def test_replay_rejects_targets_outside_allowed_origins() -> None:
    with pytest.raises(ValueError, match="不在任务允许的 origin"):
        build_replay_request(
            {"url": "https://evil.example.net/api"},
            source=None,
            allowed_origins=ALLOWED,
        )


def test_replay_requires_absolute_http_target() -> None:
    with pytest.raises(ValueError, match="HTTP 或 HTTPS 绝对地址"):
        build_replay_request({"url": "/api/orders"}, source=None, allowed_origins=ALLOWED)


def test_replay_rejects_header_value_with_newline() -> None:
    with pytest.raises(ValueError, match="非法换行"):
        build_replay_request(
            {"url": "https://example.com/api", "headers": {"X-A": "a\r\nX-B: b"}},
            source=None,
            allowed_origins=ALLOWED,
        )


def test_headers_forbidden_to_scripts_do_not_reach_the_page_template() -> None:
    """Cookie、Host、Origin 由拦截层补齐；交给 fetch 会让整个请求被浏览器丢弃。"""

    safe = _script_safe_headers(
        (
            ("Accept", "application/json"),
            ("Cookie", "session=abc"),
            ("Host", "other.example.com"),
            ("Origin", "https://a.example.com"),
            ("Referer", "https://a.example.com/x"),
            ("Sec-Fetch-Mode", "cors"),
            ("Proxy-Authorization", "Basic x"),
            ("X-Trace", "probe"),
        )
    )
    assert set(safe) == {"Accept", "X-Trace"}


def test_interception_merges_overrides_over_the_real_request_headers() -> None:
    interception = ReplayInterception(
        url="https://example.com/api",
        method="POST",
        header_overrides=(("Cookie", "session=other"),),
        removed_headers=("If-None-Match",),
    )
    merged = interception.apply_request_headers(
        {"Cookie": "session=abc", "If-None-Match": "v1", "Accept": "*/*"}
    )
    by_name = {item["name"].casefold(): item["value"] for item in merged}
    assert by_name["cookie"] == "session=other"
    assert by_name["accept"] == "*/*"
    assert "if-none-match" not in by_name


def test_interception_exposes_all_response_headers_to_the_page() -> None:
    interception = ReplayInterception(
        url="https://example.com/api",
        method="GET",
        page_origin="https://app.example.com",
    )
    merged = interception.apply_response_headers([{"name": "X-Trace", "value": "t-1"}])
    by_name = {item["name"].casefold(): item["value"] for item in merged}
    assert by_name["access-control-expose-headers"] == "*"
    assert by_name["access-control-allow-origin"] == "https://app.example.com"
    assert by_name["access-control-allow-credentials"] == "true"
    assert by_name["x-trace"] == "t-1"


def test_interception_keeps_an_existing_allow_origin_header() -> None:
    interception = ReplayInterception(
        url="https://example.com/api",
        method="GET",
        page_origin="https://app.example.com",
    )
    merged = interception.apply_response_headers(
        [{"name": "Access-Control-Allow-Origin", "value": "*"}]
    )
    by_name = {item["name"].casefold(): item["value"] for item in merged}
    assert by_name["access-control-allow-origin"] == "*"


def test_interception_drops_headers_chrome_refuses_to_forward() -> None:
    """Fetch.continueRequest 直接拒绝逐跳 Header，带上 Host 会让整次重放失败。"""

    interception = ReplayInterception(url="https://example.com/api", method="GET")
    merged = interception.apply_request_headers(
        {
            "Host": "example.com",
            "Connection": "keep-alive",
            "Content-Length": "12",
            "Proxy-Authorization": "Basic x",
            "Sec-Fetch-Mode": "cors",
            "Cookie": "session=abc",
            "Accept": "*/*",
        }
    )
    assert {item["name"] for item in merged} == {"Cookie", "Accept"}


def test_host_override_is_surfaced_for_url_rewrite_instead_of_a_header() -> None:
    interception = ReplayInterception(
        url="https://example.com/api",
        method="GET",
        header_overrides=(("Host", "staging.example.com"),),
    )
    assert interception.host_override == "staging.example.com"
    assert all(item["name"].casefold() != "host" for item in interception.apply_request_headers({}))


def test_interception_matches_only_the_exact_url_and_method() -> None:
    interception = ReplayInterception(url="https://example.com/api", method="POST")
    assert interception.matches("https://example.com/api", "post") is True
    assert interception.matches("https://example.com/api", "GET") is False
    assert interception.matches("https://example.com/api?x=1", "POST") is False


def _replay(
    session: FakeReplaySession,
    router: FakeRouter | None,
    request: ReplayRequest,
    *,
    page_url: str = "https://example.com/orders",
) -> Any:
    async def scenario() -> Any:
        return await perform_replay(session, router, request, page_url=page_url)

    return asyncio.run(scenario())


def test_replay_arms_and_always_disarms_the_interception() -> None:
    session = FakeReplaySession(_ok_payload())
    router = FakeRouter()
    request = build_replay_request({}, source=SOURCE, allowed_origins=ALLOWED)

    result = _replay(session, router, request)

    assert result.success is True
    assert result.status == 200
    assert result.headers["x-trace"] == "t-1"
    assert result.body is not None and result.body.text == '{"total":2}'
    assert len(router.armed) == 1
    assert router.disarm_count == 1
    armed = router.armed[0]
    assert armed.url == request.url
    assert armed.page_origin == "https://example.com"
    assert dict(armed.header_overrides)["Cookie"] == "session=abc"


def test_replay_disarms_the_interception_even_when_the_page_raises() -> None:
    class ExplodingSession(FakeReplaySession):
        async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            raise RuntimeError("页面已关闭")

    router = FakeRouter()
    request = build_replay_request({}, source=SOURCE, allowed_origins=ALLOWED)

    with pytest.raises(RuntimeError, match="页面已关闭"):
        _replay(ExplodingSession(), router, request)
    assert router.disarm_count == 1


def test_replay_reports_fetch_rejection_as_a_failed_result() -> None:
    session = FakeReplaySession(None)
    request = build_replay_request({}, source=SOURCE, allowed_origins=ALLOWED)

    result = _replay(session, FakeRouter(), request)

    assert result.success is False
    assert "failed to fetch" in result.error
    assert result.status is None


def test_replay_template_receives_only_structured_arguments() -> None:
    session = FakeReplaySession(_ok_payload())
    request = build_replay_request(
        {"headers": {"X-Trace": "probe"}},
        source=SOURCE,
        allowed_origins=ALLOWED,
    )

    _replay(session, FakeRouter(), request)

    spec = session.spec
    assert spec["url"] == request.url
    assert spec["method"] == "POST"
    assert spec["body"] == '{"page":1}'
    assert spec["headers"] == {
        "Accept": "application/json",
        "If-None-Match": 'W/"v1"',
        "X-Trace": "probe",
    }
    assert "Cookie" not in spec["headers"]


def test_replay_returns_binary_body_as_base64() -> None:
    payload = _ok_payload(
        headers=[["content-type", "image/png"]],
        body_base64=base64.b64encode(b"\x89PNG\r\n").decode("ascii"),
        byte_length=6,
    )
    session = FakeReplaySession(payload)
    request = build_replay_request(
        {"url": "https://example.com/logo.png", "method": "GET"},
        source=None,
        allowed_origins=ALLOWED,
    )

    result = _replay(session, FakeRouter(), request)

    assert result.body is not None
    assert result.body.base64_encoded is True
    assert base64.b64decode(result.body.text or "") == b"\x89PNG\r\n"


def test_replay_model_view_omits_body_and_header_values() -> None:
    session = FakeReplaySession(_ok_payload())
    request = build_replay_request({}, source=SOURCE, allowed_origins=ALLOWED)

    result = _replay(session, FakeRouter(), request)
    model_text = json.dumps(result.model_dict(), ensure_ascii=False)

    assert "total" not in model_text
    assert "t-1" not in model_text
    assert result.model_dict()["response_header_names"] == ["content-type", "x-trace"]


def test_replay_works_without_a_router_but_loses_restricted_headers() -> None:
    session = FakeReplaySession(_ok_payload())
    request = build_replay_request({}, source=SOURCE, allowed_origins=ALLOWED)

    result = _replay(session, None, request)

    assert result.success is True
    assert "Cookie" not in session.spec["headers"]

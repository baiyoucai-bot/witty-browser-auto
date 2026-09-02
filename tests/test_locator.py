from __future__ import annotations

import asyncio
from typing import Any

import pytest

from witty_browser_auto.agent.locator_tools import build_locator_command, locator_recipe
from witty_browser_auto.browser.frames import FrameHandle
from witty_browser_auto.browser.locator import resolve_explicit_locator
from witty_browser_auto.domain.errors import CdpCommandError, TargetNotFoundError
from witty_browser_auto.domain.models import (
    ExecutionScope,
    ExpectedCondition,
    LocatorRecipe,
    ModelToolCall,
    TaskSpec,
)


class LocatorSession:
    target_id = "page"
    session_id = "session"

    def __init__(
        self,
        *,
        search_hits: tuple[str, ...] = ("element",),
        semantic_count: int = 1,
    ) -> None:
        # DOM.performSearch 的命中既可能是文本节点，也可能落在别的帧里，
        # 所以搜索结果用命中种类描述："element" / "text" / "other_frame"。
        self.search_hits = search_hits
        self.semantic_count = semantic_count
        self.released: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _hit(self, object_id: str) -> str:
        return self.search_hits[int(object_id.removeprefix("node-object-"))]

    async def call(
        self, method: str, params: dict[str, Any] | None = None, **_: Any
    ) -> dict[str, Any]:
        values = params or {}
        self.calls.append((method, values))
        if method == "DOM.performSearch":
            return {"searchId": "search-1", "resultCount": len(self.search_hits)}
        if method == "DOM.getSearchResults":
            return {"nodeIds": [11 + index for index in range(len(self.search_hits))]}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"node-object-{int(values['nodeId']) - 11}"}}
        if method == "Runtime.releaseObject":
            self.released.append(str(values["objectId"]))
            return {}
        if method == "DOM.getBoxModel":
            raise AssertionError("坐标必须统一走 getBoundingClientRect，盒模型语义按帧类型而变")
        if method == "Runtime.evaluate":
            return {"result": {"objectId": "document-1"}}
        if method == "Runtime.callFunctionOn":
            return self._call_function_on(values)
        return {}

    def _call_function_on(self, values: dict[str, Any]) -> dict[str, Any]:
        function = str(values.get("functionDeclaration", ""))
        if "getRootNode" in function:
            hit = self._hit(str(values["arguments"][0]["objectId"]))
            if hit == "other_frame":
                # 跨帧节点属于另一个 JavaScript world，Chrome 直接拒绝这次调用。
                raise CdpCommandError("Argument should belong to the same JavaScript world")
            return {"result": {"value": hit == "element"}}
        if "implicitRole" in function or "snapshotLength" in function:
            mode = values.get("arguments", [])[-1].get("value")
            if mode == "count":
                return {"result": {"value": self.semantic_count}}
            return {"result": {"objectId": "object-1"}}
        if "getBoundingClientRect" in function and "return {x:" in function:
            return {"result": {"value": {"x": 0, "y": 0, "width": 100, "height": 30}}}
        if "getBoundingClientRect" in function or "elementFromPoint" in function:
            return {"result": {"value": True}}
        return {
            "result": {
                "value": {
                    "role": "button",
                    "name": "提交",
                    "text": "提交",
                    "disabled": False,
                    "attrs": {"target": "", "href": "", "type": "button"},
                }
            }
        }


def _main_frame(session: LocatorSession) -> FrameHandle:
    return FrameHandle(
        frame_id="main",
        session=session,  # type: ignore[arg-type]
        document_object_id="document-1",
        offset_x=0.0,
        offset_y=0.0,
        is_main=True,
        cross_origin=False,
    )


def _child_frame(session: LocatorSession, *, offset: tuple[float, float]) -> FrameHandle:
    return FrameHandle(
        frame_id="child",
        session=session,  # type: ignore[arg-type]
        document_object_id="frame-document-1",
        offset_x=offset[0],
        offset_y=offset[1],
        is_main=False,
        cross_origin=True,
    )


def test_css_locator_resolves_strict_target_and_cleans_search() -> None:
    async def scenario() -> None:
        session = LocatorSession()
        candidate, box, object_id = await resolve_explicit_locator(
            _main_frame(session),
            LocatorRecipe(
                "explicit_css",
                '{"value":"button[data-testid=submit]","exact":true,"index":0,'
                '"index_explicit":false,"timeout_seconds":1}',
            ),
        )
        assert object_id == "node-object-0"
        assert box.width == 100
        assert candidate.role == "button"
        assert candidate.name == "提交"
        assert [method for method, _ in session.calls] == [
            "DOM.getDocument",
            "DOM.performSearch",
            "DOM.getSearchResults",
            "DOM.resolveNode",
            "Runtime.callFunctionOn",
            "DOM.discardSearchResults",
            "DOM.scrollIntoViewIfNeeded",
            "Runtime.callFunctionOn",
            "Runtime.callFunctionOn",
            "Runtime.callFunctionOn",
            "Runtime.callFunctionOn",
        ]

    asyncio.run(scenario())


def test_locator_rejects_ambiguous_matches_without_index() -> None:
    async def scenario() -> None:
        session = LocatorSession(search_hits=("element", "element"))
        with pytest.raises(TargetNotFoundError, match="匹配到 2 个元素"):
            await resolve_explicit_locator(
                _main_frame(session),
                LocatorRecipe(
                    "explicit_xpath",
                    '{"value":"//button","exact":true,"index":0,'
                    '"index_explicit":false,"timeout_seconds":1}',
                ),
            )

    asyncio.run(scenario())


def test_css_locator_ignores_selector_text_matched_inside_the_document() -> None:
    """DOM.performSearch 同时做纯文本匹配，样式表里的选择器字面量不能算作匹配。"""

    async def scenario() -> None:
        # 第一个结果是样式表里的 `#row { ... }` 文本节点，第二个才是真正的元素。
        session = LocatorSession(search_hits=("text", "element"))
        _, _, object_id = await resolve_explicit_locator(
            _main_frame(session),
            LocatorRecipe(
                "explicit_css",
                '{"value":"#row","exact":true,"index":0,'
                '"index_explicit":false,"timeout_seconds":1}',
            ),
        )
        assert object_id == "node-object-1"
        assert session.released == ["node-object-0"]

    asyncio.run(scenario())


def test_main_frame_locator_does_not_reach_into_iframes() -> None:
    """performSearch 横跨所有帧，但 iframe 内的元素必须由带 frame_id 的定位器负责。

    否则作用域随页面结构漂移，而且拿到的句柄和主框架的坐标换算对不上。
    """

    async def scenario() -> None:
        session = LocatorSession(search_hits=("other_frame",))
        with pytest.raises(TargetNotFoundError, match="尚未匹配元素"):
            await resolve_explicit_locator(
                _main_frame(session),
                LocatorRecipe(
                    "explicit_css",
                    '{"value":"#card","exact":true,"index":0,'
                    '"index_explicit":false,"timeout_seconds":0.2}',
                ),
            )
        assert "node-object-0" in session.released

    asyncio.run(scenario())


def test_main_frame_locator_still_reaches_into_shadow_dom() -> None:
    """穿透 shadow DOM 是 performSearch 相对 querySelectorAll 的价值，不能被帧过滤误伤。"""

    async def scenario() -> None:
        # shadow 节点的 composed 根就是宿主文档，因此归属判定为真。
        session = LocatorSession(search_hits=("element",))
        _, _, object_id = await resolve_explicit_locator(
            _main_frame(session),
            LocatorRecipe(
                "explicit_css",
                '{"value":"#shadow-inner","exact":true,"index":0,'
                '"index_explicit":false,"timeout_seconds":1}',
            ),
        )
        assert object_id == "node-object-0"
        assert session.released == []

    asyncio.run(scenario())


def test_role_locator_resolves_by_accessible_name() -> None:
    async def scenario() -> None:
        session = LocatorSession()
        candidate, box, object_id = await resolve_explicit_locator(
            _main_frame(session),
            LocatorRecipe(
                "explicit_role",
                '{"value":"button","name":"提交","exact":true,"index":0,'
                '"index_explicit":false,"timeout_seconds":1}',
            ),
        )

        assert object_id == "object-1"
        assert candidate.name == "提交"
        assert box.height == 30
        # 语义定位在帧文档上执行，这样同一份模板对主框架和 iframe 行为一致。
        assert [method for method, _ in session.calls[:2]] == [
            "Runtime.callFunctionOn",
            "Runtime.callFunctionOn",
        ]
        assert session.calls[0][1]["objectId"] == "document-1"

    asyncio.run(scenario())


def test_frame_locator_scopes_query_to_frame_document_and_offsets_box() -> None:
    """iframe 内定位必须在帧文档上查询，并把盒模型换算回主框架视口坐标。"""

    async def scenario() -> None:
        session = LocatorSession()
        _candidate, box, _object_id = await resolve_explicit_locator(
            _child_frame(session, offset=(400.0, 300.0)),
            LocatorRecipe(
                "explicit_css",
                '{"value":"button.submit","exact":true,"index":0,'
                '"index_explicit":false,"timeout_seconds":1}',
                frame_id="child",
            ),
        )

        # 跨站 iframe 的盒模型是帧内局部坐标，派发输入前必须叠加 iframe 自身的偏移。
        assert (box.x, box.y) == (400.0, 300.0)
        assert (box.width, box.height) == (100, 30)
        methods = [method for method, _ in session.calls]
        assert "DOM.performSearch" not in methods
        query_calls = [
            params
            for method, params in session.calls
            if method == "Runtime.callFunctionOn"
            and "snapshotLength" in str(params.get("functionDeclaration", ""))
        ]
        assert query_calls and all(
            params["objectId"] == "frame-document-1" for params in query_calls
        )

    asyncio.run(scenario())


def test_locator_tool_builds_task_input_command() -> None:
    task = TaskSpec(
        "locator-input",
        "填写账号",
        "https://example.com",
        ExecutionScope("project"),
        inputs={"account": "account-value"},
    )
    command, input_key = build_locator_command(
        ModelToolCall(
            "call-1",
            "input_text_locator",
            {
                "locator": {"strategy": "label", "value": "账号"},
                "input_key": "account",
            },
        ),
        task,
        "action-1",
        None,
    )

    assert input_key == "account"
    assert command.value == "account-value"
    assert command.target_id is None
    assert command.locator is not None
    assert command.locator.strategy == "explicit_label"


def test_locator_tool_rejects_observation_target_as_postcondition() -> None:
    call = ModelToolCall(
        "call-2",
        "click_locator",
        {"locator": {"strategy": "css", "value": "button.submit"}},
    )
    task = TaskSpec(
        "locator-click",
        "提交",
        "https://example.com",
        ExecutionScope("project"),
    )

    with pytest.raises(ValueError, match="不能用 target_exists"):
        build_locator_command(
            call,
            task,
            "action-2",
            ExpectedCondition("target_exists", "target-current"),
        )


def test_locator_recipe_requires_explicit_index_for_ambiguous_runtime_matches() -> None:
    recipe = locator_recipe({"locator": {"strategy": "xpath", "value": "//button", "index": 1}})

    assert recipe.strategy == "explicit_xpath"
    assert '"index_explicit":true' in (recipe.value or "")

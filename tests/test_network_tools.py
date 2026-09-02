from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from witty_browser_auto.agent.network_tools import NETWORK_TOOL_SCHEMAS, execute_network_tool
from witty_browser_auto.agent.tools import ToolExecutor
from witty_browser_auto.domain.models import (
    ActionReceipt,
    BoundingBox,
    CandidateTarget,
    ExecutionScope,
    LocatorRecipe,
    ModelToolCall,
    Observation,
    TaskSpec,
    VerificationResult,
)
from witty_browser_auto.domain.network_data import NetworkDataExportResult


class FakeNetworkExtractor:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.exported_candidate_ids: tuple[str, ...] = ()
        self.route_operations: list[tuple[str, dict[str, Any]]] = []

    async def inspect(self, *, max_candidates: int = 20) -> dict[str, Any]:
        return {
            "captured_count": 1,
            "transport": "current_browser_cdp",
            "session_reused": True,
            "active_request_count": 0,
            "candidates": [
                {
                    "candidate_id": "candidate-1",
                    "endpoint": "https://example.com/api/orders",
                    "method": "GET",
                    "status": 200,
                    "mime_type": "application/json",
                    "resource_type": "Fetch",
                    "body_bytes": 128,
                    "score": 100,
                    "json_shape": {"type": "object", "keys": ["orders"]},
                    "sample": "SENSITIVE-NETWORK-RECORD",
                }
            ][:max_candidates],
        }

    async def export(self, candidate_id: str, collection_name: str) -> NetworkDataExportResult:
        json_path = self.tmp_path / "network.json"
        csv_path = self.tmp_path / "network.csv"
        json_path.write_text("{}", encoding="utf-8")
        csv_path.write_text("id\n1\n", encoding="utf-8")
        return NetworkDataExportResult(
            candidate_id=candidate_id,
            collection_name=collection_name,
            endpoint="https://example.com/api/orders",
            byte_count=2,
            body_sha256="a" * 64,
            record_count=1,
            json_path=json_path,
            csv_path=csv_path,
        )

    async def export_many(
        self,
        candidate_ids: tuple[str, ...],
        collection_name: str,
    ) -> NetworkDataExportResult:
        self.exported_candidate_ids = tuple(candidate_ids)
        json_path = self.tmp_path / "network-many.json"
        csv_path = self.tmp_path / "network-many.csv"
        json_path.write_text("{}", encoding="utf-8")
        csv_path.write_text("id\n1\n2\n", encoding="utf-8")
        return NetworkDataExportResult(
            candidate_id="batch-1",
            collection_name=collection_name,
            endpoint="https://example.com/api/orders",
            byte_count=4,
            body_sha256="b" * 64,
            record_count=2,
            json_path=json_path,
            csv_path=csv_path,
            complete=True,
            captured_response_count=2,
            visited_pages=(1, 2),
            declared_total=2,
            declared_pages=2,
            completion_evidence=("接口声明总数 2 与聚合去重记录数一致",),
            failure_reasons=(),
        )

    async def manage_route(
        self,
        operation: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        self.route_operations.append((operation, config))
        return {
            "rules": [
                {
                    "rule_id": "route-1",
                    "url_pattern": config.get("url_pattern", "https://example.com/api/*"),
                    "action": config.get("action", "block"),
                    "request_header_names": ["X-Test"],
                    "request_body_bytes": 0,
                    "response_header_names": [],
                    "response_body_bytes": 0,
                }
            ]
        }


class ExpiredNetworkExtractor(FakeNetworkExtractor):
    async def export(self, candidate_id: str, collection_name: str) -> NetworkDataExportResult:
        raise ValueError("网络接口候选不存在或已经过期，请重新观察")


class MixedNetworkExtractor(FakeNetworkExtractor):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.export_many_called = False

    async def inspect(self, *, max_candidates: int = 20) -> dict[str, Any]:
        candidates = [
            {
                "candidate_id": "candidate-orders",
                "endpoint": "https://example.com/api/orders",
                "method": "GET",
                "status": 200,
                "mime_type": "application/json",
                "resource_type": "Fetch",
                "body_bytes": 128,
                "score": 100,
                "json_shape": {"type": "object", "record_path": ["orders"]},
            },
            {
                "candidate_id": "candidate-detail",
                "endpoint": "https://example.com/api/order/detail",
                "method": "GET",
                "status": 200,
                "mime_type": "application/json",
                "resource_type": "Fetch",
                "body_bytes": 96,
                "score": 90,
                "json_shape": {"type": "object", "record_path": ["details"]},
            },
        ]
        return {
            "captured_count": len(candidates),
            "transport": "current_browser_cdp",
            "session_reused": True,
            "active_request_count": 0,
            "candidates": candidates[:max_candidates],
        }

    async def export_many(
        self,
        candidate_ids: tuple[str, ...],
        collection_name: str,
    ) -> NetworkDataExportResult:
        self.export_many_called = True
        return await super().export_many(candidate_ids, collection_name)


class FakeStructuredExtractor:
    async def inspect(self, *, root_selector: str = "body", max_candidates: int = 12):
        assert root_selector == "body"
        return {
            "candidates": [
                {
                    "row_selector": "table.orders > tbody > tr",
                    "row_count": 10,
                    "child_hints": [
                        {
                            "selector": ":scope > td:nth-of-type(1)",
                            "label": "订单号",
                            "source_options": ["text"],
                        }
                    ],
                }
            ][:max_candidates]
        }

    async def extract(self, spec):  # pragma: no cover - this test only exercises inspection
        raise AssertionError("不应执行 DOM 采集")


class PageActionDriver:
    async def execute(self, command):
        return ActionReceipt(command.action_id, True, True, "动作已执行", 1.0)

    async def verify(self, condition):
        return VerificationResult(True, "页面已变化")


def test_network_tool_schemas_are_read_only_and_bounded() -> None:
    schemas = {item["function"]["name"]: item["function"] for item in NETWORK_TOOL_SCHEMAS}

    assert set(schemas) == {
        "inspect_network_data",
        "export_network_response",
        "wait_network_response",
        "manage_network_route",
    }
    assert schemas["inspect_network_data"]["parameters"]["properties"]["max_candidates"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 50,
    }
    assert schemas["export_network_response"]["parameters"]["additionalProperties"] is False
    wait_properties = schemas["wait_network_response"]["parameters"]["properties"]
    assert wait_properties["url_substring"]["maxLength"] == 500
    assert wait_properties["timeout_seconds"]["maximum"] == 300
    assert schemas["wait_network_response"]["parameters"]["required"] == ["url_substring"]
    route_properties = schemas["manage_network_route"]["parameters"]["properties"]
    assert "request_header_input_keys" in route_properties
    assert "response_header_input_keys" in route_properties


def test_network_tools_strip_samples_and_only_return_export_summary(tmp_path: Path) -> None:
    async def scenario() -> None:
        extractor = FakeNetworkExtractor(tmp_path)

        inspection = await execute_network_tool(
            "inspect_network_data",
            {"max_candidates": 5},
            extractor,
            task_inputs={"account": "SENSITIVE-ACCOUNT"},
        )
        exported = await execute_network_tool(
            "export_network_response",
            {"candidate_id": "candidate-1", "collection_name": "订单接口"},
            extractor,
            task_inputs={"account": "SENSITIVE-ACCOUNT"},
        )

        assert inspection.success is True
        assert "SENSITIVE-NETWORK-RECORD" not in str(inspection.data)
        assert inspection.data["session_reused"] is True
        assert inspection.data["active_request_count"] == 0
        assert exported.success is True
        assert exported.evidence is not None
        assert exported.data["record_count"] == 1
        assert exported.data["complete"] is False
        assert exported.data["captured_response_count"] == 1
        assert any("单个" in reason for reason in exported.data["failure_reasons"])
        assert "network.json" in exported.data["json_path"]

    asyncio.run(scenario())


class WaitingNetworkExtractor(FakeNetworkExtractor):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.wait_calls: list[tuple[str, float]] = []
        self.wait_result: dict[str, Any] = {
            "matched": True,
            "captured": True,
            "candidate_id": "candidate-1",
            "endpoint": "https://example.com/api/orders",
            "method": "POST",
            "status": 200,
            "mime_type": "application/json",
            "body_bytes": 128,
            "duration_ms": 210,
        }

    async def wait_for_response(
        self,
        url_substring: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        self.wait_calls.append((url_substring, timeout_seconds))
        return dict(self.wait_result)


def test_wait_network_response_tool_returns_wait_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        extractor = WaitingNetworkExtractor(tmp_path)

        outcome = await execute_network_tool(
            "wait_network_response",
            {"url_substring": "/api/orders", "timeout_seconds": 12},
            extractor,
            task_inputs={},
        )
        assert outcome.success is True
        assert outcome.idempotent is True
        assert outcome.counts_as_action is False
        assert outcome.data["candidate_id"] == "candidate-1"
        assert outcome.data["duration_ms"] == 210
        assert "candidate_id" in outcome.message or "捕获" in outcome.message
        assert extractor.wait_calls == [("/api/orders", 12.0)]

        extractor.wait_result = {"matched": False, "url_substring": "/api/none"}
        timeout_outcome = await execute_network_tool(
            "wait_network_response",
            {"url_substring": "/api/none"},
            extractor,
            task_inputs={},
        )
        assert timeout_outcome.success is True
        assert "超时" in timeout_outcome.message
        assert extractor.wait_calls[-1] == ("/api/none", 30.0)

    asyncio.run(scenario())


def test_wait_network_response_tool_rejects_invalid_arguments(tmp_path: Path) -> None:
    async def scenario() -> None:
        extractor = WaitingNetworkExtractor(tmp_path)
        for arguments in (
            {"url_substring": ""},
            {"url_substring": "/api/orders", "timeout_seconds": 0},
            {"url_substring": "/api/orders", "timeout_seconds": 301},
            {"url_substring": "/api/orders", "unknown": True},
        ):
            try:
                await execute_network_tool(
                    "wait_network_response",
                    arguments,
                    extractor,
                    task_inputs={},
                )
            except ValueError:
                continue
            raise AssertionError(f"非法参数不应被接受：{arguments}")
        assert extractor.wait_calls == []

        plain = FakeNetworkExtractor(tmp_path)
        try:
            await execute_network_tool(
                "wait_network_response",
                {"url_substring": "/api/orders"},
                plain,
                task_inputs={},
            )
        except ValueError as exc:
            assert "不支持等待网络响应" in str(exc)
        else:  # pragma: no cover - assertion branch
            raise AssertionError("缺少等待能力的提取器不应通过")

    asyncio.run(scenario())


def test_network_route_tool_executes_without_exposing_header_values(tmp_path: Path) -> None:
    async def scenario() -> None:
        extractor = FakeNetworkExtractor(tmp_path)

        outcome = await execute_network_tool(
            "manage_network_route",
            {
                "operation": "add",
                "url_pattern": "https://example.com/api/*",
                "action": "modify_request",
                "request_headers": {"X-Test": "private-value"},
            },
            extractor,
            task_inputs={},
        )

        assert outcome.success is True
        assert outcome.idempotent is False
        assert outcome.counts_as_action is True
        assert extractor.route_operations[0][0] == "add"
        assert "private-value" not in str(outcome.data)

    asyncio.run(scenario())


def test_network_route_injects_sensitive_headers_from_task_inputs(tmp_path: Path) -> None:
    async def scenario() -> None:
        extractor = FakeNetworkExtractor(tmp_path)
        secret_values = {
            "auth": "Bearer private-auth",
            "session": "session=private-cookie",
            "virtual_host": "api.internal.example",
        }

        outcome = await execute_network_tool(
            "manage_network_route",
            {
                "operation": "add",
                "url_pattern": "https://example.com/api/*",
                "action": "modify_request",
                "request_header_input_keys": {
                    "Authorization": "auth",
                    "Cookie": "session",
                    "Host": "virtual_host",
                },
            },
            extractor,
            task_inputs=secret_values,
        )

        _, config = extractor.route_operations[0]
        assert config["request_headers"] == {
            "Authorization": secret_values["auth"],
            "Cookie": secret_values["session"],
            "Host": secret_values["virtual_host"],
        }
        assert "request_header_input_keys" not in config
        assert all(value not in str(outcome.data) for value in secret_values.values())

    asyncio.run(scenario())


def test_network_route_rejects_literal_sensitive_headers_and_missing_input_key(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        extractor = FakeNetworkExtractor(tmp_path)
        for header in ("Authorization", "Cookie", "Host", "X-API-Key", "X-Access-Token"):
            try:
                await execute_network_tool(
                    "manage_network_route",
                    {
                        "operation": "add",
                        "url_pattern": "https://example.com/api/*",
                        "action": "modify_request",
                        "request_headers": {header: "literal-secret"},
                    },
                    extractor,
                    task_inputs={},
                )
            except ValueError as exc:
                assert "必须通过任务 input_key 注入" in str(exc)
            else:  # pragma: no cover - assertion branch
                raise AssertionError(f"敏感 Header {header} 不应接受字面值")

        try:
            await execute_network_tool(
                "manage_network_route",
                {
                    "operation": "add",
                    "url_pattern": "https://example.com/api/*",
                    "action": "modify_request",
                    "request_header_input_keys": {"Authorization": "missing"},
                },
                extractor,
                task_inputs={},
            )
        except ValueError as exc:
            assert "任务输入键不存在" in str(exc)
        else:  # pragma: no cover - assertion branch
            raise AssertionError("不存在的任务输入键不应被接受")

    asyncio.run(scenario())


def test_network_route_change_reopens_page_action_stage(tmp_path: Path) -> None:
    async def scenario() -> None:
        task = TaskSpec(
            "route-stage",
            "修改接口后重新触发查询",
            "https://example.com/orders",
            ExecutionScope("project"),
        )
        extractor = FakeNetworkExtractor(tmp_path)
        executor = ToolExecutor(
            object(),  # type: ignore[arg-type]
            task,
            network_data_extractor=extractor,
        )
        executor.network_data_inspected = True
        executor.network_inspection = {"candidates": [{"candidate_id": "old"}]}
        observation = Observation("surface", task.start_url, "订单", 1, "orders", "订单页", ())

        outcome = await executor.execute(
            ModelToolCall(
                "route-add",
                "manage_network_route",
                {
                    "operation": "add",
                    "url_pattern": "https://example.com/api/*",
                    "action": "block",
                },
            ),
            observation,
        )

        assert outcome.success is True
        assert executor.network_data_inspected is False
        assert executor.network_inspection == {}
        assert executor.network_data_exhausted is False

    asyncio.run(scenario())


def test_page_action_invalidates_old_network_and_dom_observations(tmp_path: Path) -> None:
    class PageAwareNetworkExtractor(FakeNetworkExtractor):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.inspections = 0

        async def inspect(self, *, max_candidates: int = 20) -> dict[str, Any]:
            self.inspections += 1
            candidates = [
                {
                    "candidate_id": "home",
                    "endpoint": "https://example.com/api/site",
                    "json_shape": {"type": "object", "record_path": ["site"]},
                }
            ]
            if self.inspections > 1:
                candidates.append(
                    {
                        "candidate_id": "orders",
                        "endpoint": "https://example.com/api/orders",
                        "json_shape": {"type": "object", "record_path": ["orders"]},
                    }
                )
            return {"captured_count": len(candidates), "candidates": candidates[:max_candidates]}

    async def scenario() -> None:
        task = TaskSpec(
            "page-change-network",
            "点击订单入口后获取全部订单",
            "https://example.com",
            ExecutionScope("project"),
        )
        executor = ToolExecutor(
            PageActionDriver(),  # type: ignore[arg-type]
            task,
            network_data_extractor=PageAwareNetworkExtractor(tmp_path),
        )
        target = CandidateTarget(
            "orders-entry",
            "button",
            "订单查询",
            "订单查询",
            0.99,
            ("当前页面入口",),
            LocatorRecipe("test", role="button", name="订单查询"),
            BoundingBox(0, 0, 100, 30),
        )
        observation = Observation("surface", task.start_url, "首页", 1, "home", "首页", (target,))

        await executor.execute(
            ModelToolCall("inspect-home", "inspect_network_data", {}), observation
        )
        executor.collection_inspected = True
        executor.collection_inspection = {"candidates": [{"candidate_id": "old-dom"}]}
        clicked = await executor.execute(
            ModelToolCall(
                "click-entry",
                "click",
                {
                    "target_id": "orders-entry",
                    "expect_kind": "fingerprint_changed",
                    "expect_value": "home",
                },
            ),
            observation,
        )

        assert clicked.success is True
        assert executor.network_data_inspected is False
        assert executor.network_data_exhausted is False
        assert executor.collection_inspected is False
        assert executor.collection_inspection is None

        refreshed = await executor.execute(
            ModelToolCall("inspect-orders", "inspect_network_data", {}), observation
        )
        assert [item["candidate_id"] for item in refreshed.data["candidates"]] == ["orders"]

    asyncio.run(scenario())


def test_network_tool_accepts_multiple_candidate_ids_and_returns_closed_result(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        extractor = FakeNetworkExtractor(tmp_path)

        exported = await execute_network_tool(
            "export_network_response",
            {
                "candidate_ids": ["candidate-1", "candidate-2"],
                "collection_name": "全部订单详情",
            },
            extractor,
            task_inputs={},
        )

        assert exported.success is True
        assert exported.data["complete"] is True
        assert exported.data["captured_response_count"] == 2
        assert extractor.exported_candidate_ids == ("candidate-1", "candidate-2")

    asyncio.run(scenario())


def test_failed_network_export_reopens_inspection_stage(tmp_path: Path) -> None:
    async def scenario() -> None:
        task = TaskSpec(
            "expired-network-candidate",
            "导出接口数据",
            "https://example.com/orders",
            ExecutionScope("project"),
        )
        executor = ToolExecutor(
            object(),  # type: ignore[arg-type]
            task,
            network_data_extractor=ExpiredNetworkExtractor(tmp_path),
        )
        observation = Observation(
            "surface",
            task.start_url,
            "订单",
            1,
            "orders",
            "订单页",
            (),
        )

        inspected = await executor.execute(
            ModelToolCall("inspect", "inspect_network_data", {"max_candidates": 5}),
            observation,
        )
        expired = await executor.execute(
            ModelToolCall(
                "export",
                "export_network_response",
                {"candidate_id": "expired", "collection_name": "订单"},
            ),
            observation,
        )

        assert inspected.success is True
        assert expired.success is False
        assert executor.network_data_inspected is False

    asyncio.run(scenario())


def test_mixed_network_batch_switches_to_dom_without_repeating_export(tmp_path: Path) -> None:
    async def scenario() -> None:
        task = TaskSpec(
            "mixed-network-candidates",
            "获取全部订单和详情",
            "https://example.com/orders",
            ExecutionScope("project"),
        )
        extractor = MixedNetworkExtractor(tmp_path)
        executor = ToolExecutor(
            object(),  # type: ignore[arg-type]
            task,
            network_data_extractor=extractor,
            structured_extractor=FakeStructuredExtractor(),
        )
        observation = Observation("surface", task.start_url, "订单", 1, "orders", "订单页", ())

        inspected = await executor.execute(
            ModelToolCall("inspect", "inspect_network_data", {"max_candidates": 5}),
            observation,
        )
        rejected = await executor.execute(
            ModelToolCall(
                "export",
                "export_network_response",
                {
                    "candidate_ids": ["candidate-orders", "candidate-detail"],
                    "collection_name": "全部订单详情",
                },
            ),
            observation,
        )

        assert inspected.success is True
        assert rejected.success is False
        assert extractor.export_many_called is False
        assert executor.network_data_inspected is False
        assert executor.network_data_exhausted is True
        assert executor.collection_candidate_ids == ("collection_1",)
        assert rejected.data["dom_fallback"]["candidate_ids"] == ["collection_1"]

    asyncio.run(scenario())


def test_incomplete_network_export_is_not_offered_again(tmp_path: Path) -> None:
    async def scenario() -> None:
        task = TaskSpec(
            "incomplete-network-candidate",
            "导出全部接口数据",
            "https://example.com/orders",
            ExecutionScope("project"),
        )
        executor = ToolExecutor(
            object(),  # type: ignore[arg-type]
            task,
            network_data_extractor=FakeNetworkExtractor(tmp_path),
            structured_extractor=FakeStructuredExtractor(),
        )
        observation = Observation("surface", task.start_url, "订单", 1, "orders", "订单页", ())

        await executor.execute(
            ModelToolCall("inspect-1", "inspect_network_data", {"max_candidates": 5}),
            observation,
        )
        exported = await executor.execute(
            ModelToolCall(
                "export",
                "export_network_response",
                {"candidate_id": "candidate-1", "collection_name": "订单"},
            ),
            observation,
        )
        reinspected = await executor.execute(
            ModelToolCall("inspect-2", "inspect_network_data", {"max_candidates": 5}),
            observation,
        )

        assert exported.success is True
        assert executor.network_data_inspected is False
        assert executor.network_data_exhausted is True
        assert executor.network_candidate_ids == ()
        assert executor.collection_candidate_ids == ("collection_1",)
        assert exported.data["dom_fallback"]["candidate_ids"] == ["collection_1"]
        assert reinspected.data["candidates"] == []

    asyncio.run(scenario())

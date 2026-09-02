"""采集程序「编译-验证门-零模型重放」P0 行为回归。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from witty_browser_auto.agent.collection_program import (
    scenario_key,
    spec_contains_task_inputs,
    verify_and_promote_collection_program,
)
from witty_browser_auto.agent.tools import ToolExecutor
from witty_browser_auto.browser.extraction import CdpDomCollectionExtractor
from witty_browser_auto.domain.extraction import (
    CollectionExtractionResult,
    CollectionExtractionSpec,
    collection_structure_fingerprint,
    evaluate_entry_probe,
)
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionKind,
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
from witty_browser_auto.memory.background import shared_background_memory
from witty_browser_auto.memory.store import SqliteUrlMemoryStore
from witty_browser_auto.toolkit.facade import BrowserToolkit

_SPEC_MAPPING: dict[str, Any] = {
    "collection_name": "订单",
    "row_selector": ".order-row",
    "fields": [
        {"name": "订单号", "selector": ".id"},
        {"name": "状态", "selector": ".status"},
    ],
    "unique_key": "订单号",
    "pagination_mode": "next",
    "next_page_selector": ".next",
    "total_count_selector": ".total",
    "page_wait_timeout_seconds": 0.05,
}


def _spec(**overrides: Any) -> CollectionExtractionSpec:
    return CollectionExtractionSpec.from_mapping({**_SPEC_MAPPING, **overrides})


def _healthy_probe(spec: CollectionExtractionSpec) -> dict[str, Any]:
    return {
        "row_count": 10,
        "unique_key_filled_count": 10,
        "field_non_empty": {field.name: 10 for field in spec.fields},
        "declared_total": 80,
        "declared_pages": None,
        "current_page": None,
        "pagination_exists": True,
        "pagination_disabled": False,
    }


def _complete_result(
    artifact_root: Path,
    spec: CollectionExtractionSpec,
) -> CollectionExtractionResult:
    artifact_root.mkdir(parents=True, exist_ok=True)
    json_path = artifact_root / "orders.json"
    csv_path = artifact_root / "orders.csv"
    json_path.write_text('{"items": []}', encoding="utf-8")
    csv_path.write_text("订单号,状态\n", encoding="utf-8")
    return CollectionExtractionResult(
        collection_name=spec.collection_name,
        complete=True,
        unique_count=80,
        exported_count=80,
        duplicate_count=0,
        visited_pages=tuple(range(1, 9)),
        failed_pages=(),
        declared_total=80,
        declared_pages=None,
        completion_evidence=("页面声明总数与代码去重计数一致",),
        failure_reasons=(),
        json_path=json_path,
        csv_path=csv_path,
        pagination_mode=spec.pagination_mode,
    )


class ListPageDriver:
    """始终停留在订单列表页的最小驱动，覆盖引擎观察、动作与证据接口。"""

    capabilities = DriverCapabilities(dom=True, accessibility=True, javascript=True)

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root
        self.url = ""
        self.executed: list[ActionCommand] = []

    async def start(self) -> None:
        return None

    async def open(self, url: str) -> str:
        self.url = url
        return "surface-1"

    async def observe(self, *, force: bool = False) -> Observation:
        return Observation(
            surface_id="surface-1",
            url=self.url,
            title="订单列表",
            version=1,
            fingerprint="orders-list",
            summary="订单列表页",
            candidates=(
                CandidateTarget(
                    target_id="target-rows",
                    role="table",
                    name="订单列表",
                    text="订单",
                    confidence=0.9,
                    reasons=("测试候选",),
                    recipe=LocatorRecipe("fake", role="table", name="订单列表"),
                    box=BoundingBox(0, 0, 400, 300),
                ),
            ),
        )

    async def execute(self, command: ActionCommand) -> ActionReceipt:
        self.executed.append(command)
        return ActionReceipt(command.action_id, True, True, "动作已执行", 1.0)

    async def verify(self, condition: ExpectedCondition) -> VerificationResult:
        return VerificationResult(False, "未验证")

    async def capture_evidence(self, label: str) -> Path:
        path = self.artifact_root / f"{label}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-png")
        return path

    async def close(self) -> None:
        return None


class ReplayableExtractor:
    """实现 probe_entry 的结构化采集器 Fake，供验证门与重放链路使用。"""

    def __init__(self, artifact_root: Path, *, probe_row_count: int = 10) -> None:
        self.artifact_root = artifact_root
        self.probe_row_count = probe_row_count
        self.probe_count = 0
        self.inspection_count = 0
        self.specs: list[CollectionExtractionSpec] = []

    async def inspect(
        self,
        *,
        root_selector: str = "body",
        max_candidates: int = 12,
    ) -> dict[str, object]:
        self.inspection_count += 1
        return {
            "candidates": [
                {
                    "row_selector": ".order-row",
                    "row_count": 10,
                    "child_hints": [
                        {
                            "selector": ".id",
                            "label": "订单号",
                            "source_options": ["text"],
                        },
                        {
                            "selector": ".status",
                            "label": "状态",
                            "source_options": ["text"],
                        },
                    ],
                }
            ][:max_candidates]
        }

    async def probe_entry(self, spec: CollectionExtractionSpec) -> dict[str, Any]:
        self.probe_count += 1
        probe = _healthy_probe(spec)
        probe["row_count"] = self.probe_row_count
        probe["unique_key_filled_count"] = self.probe_row_count
        return probe

    async def extract(self, spec: CollectionExtractionSpec) -> CollectionExtractionResult:
        self.specs.append(spec)
        return _complete_result(self.artifact_root, spec)


class GateHost:
    def __init__(self, driver: Any, memory_runtime: Any, extractor: Any) -> None:
        self.driver = driver
        self.memory_runtime = memory_runtime
        self.structured_extractor = extractor
        self.events: list[tuple[str, str]] = []

    async def emit_program_event(
        self,
        kind: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.events.append((kind, message))


class RecordingDriver:
    def __init__(self) -> None:
        self.commands: list[ActionCommand] = []

    async def execute(self, command: ActionCommand) -> ActionReceipt:
        self.commands.append(command)
        return ActionReceipt(command.action_id, True, True, "动作已执行", 1.0)


def test_spec_round_trips_through_to_mapping() -> None:
    spec = _spec(
        filters=[{"field": "状态", "operator": "equals", "value": "已完成"}],
        detail_trigger_selector=".detail",
    )
    rebuilt = CollectionExtractionSpec.from_mapping(spec.to_mapping())
    assert rebuilt == spec


def test_structure_fingerprint_ignores_limits_but_tracks_structure() -> None:
    base = _spec()
    assert collection_structure_fingerprint(base) == collection_structure_fingerprint(
        _spec(max_pages=7, max_items=99, page_wait_timeout_seconds=1.5)
    )
    assert collection_structure_fingerprint(base) != collection_structure_fingerprint(
        _spec(row_selector=".order-line")
    )


def test_entry_probe_evaluation_covers_structure_expectations() -> None:
    spec = _spec()
    assert evaluate_entry_probe(spec, _healthy_probe(spec)) is None
    assert evaluate_entry_probe(spec, {**_healthy_probe(spec), "row_count": 0}) is not None
    assert (
        evaluate_entry_probe(spec, {**_healthy_probe(spec), "unique_key_filled_count": 3})
        is not None
    )
    assert (
        evaluate_entry_probe(
            spec,
            {**_healthy_probe(spec), "field_non_empty": {"订单号": 0, "状态": 0}},
        )
        is not None
    )
    assert evaluate_entry_probe(spec, {**_healthy_probe(spec), "declared_total": None}) is not None
    numbered = _spec(
        pagination_mode="page_number",
        next_page_selector=None,
        page_number_selector=".page",
        current_page_selector=".page.active",
        total_pages_selector=".page:last-of-type",
    )
    numbered_probe = {**_healthy_probe(numbered), "current_page": None, "declared_pages": 8}
    assert evaluate_entry_probe(numbered, numbered_probe) is not None
    numbered_probe["current_page"] = 1
    assert evaluate_entry_probe(numbered, numbered_probe) is None


def test_probe_entry_reads_structure_facts_without_pagination() -> None:
    class ProbeDriver:
        def __init__(self, page: dict[str, Any]) -> None:
            self.page = page
            self.commands: list[ActionCommand] = []

        async def execute(self, command: ActionCommand) -> ActionReceipt:
            self.commands.append(command)
            assert "WITTY_BROWSER_AUTO_EXTRACT_PAGE" in (command.script or "")
            return ActionReceipt(
                command.action_id, True, True, "ok", 1.0, data={"value": self.page}
            )

    async def scenario() -> None:
        driver = ProbeDriver(
            {
                "rows": [
                    {"订单号": "A-1", "状态": "已完成"},
                    {"订单号": "A-2", "状态": ""},
                ],
                "fingerprint": "fp-1",
                "declared_total": 2,
                "declared_pages": None,
                "current_page": None,
                "pagination_exists": True,
                "pagination_disabled": False,
            }
        )
        extractor = CdpDomCollectionExtractor(driver, Path("/tmp"))
        probe = await extractor.probe_entry(_spec())

        assert probe["row_count"] == 2
        assert probe["unique_key_filled_count"] == 2
        assert probe["field_non_empty"] == {"订单号": 2, "状态": 1}
        assert probe["declared_total"] == 2
        assert len(driver.commands) == 1

    asyncio.run(scenario())


def test_store_saves_replaces_and_degrades_programs(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteUrlMemoryStore(tmp_path / "memory.db")
        scope = ExecutionScope("project", "tenant", "account")
        spec = _spec()
        fingerprint = collection_structure_fingerprint(spec)
        saved = await store.save_collection_program(
            scope=scope,
            scenario_key="scenario-1",
            url="https://example.com/orders?page=3",
            structure_fingerprint=fingerprint,
            spec=spec.to_mapping(),
            summary={"unique_count": 80},
            evidence_id="evidence-1",
        )

        best = await store.best_collection_program(
            scope=scope,
            scenario_key="scenario-1",
            url="https://example.com/orders",
        )
        assert best is not None and best.program_id == saved.program_id
        assert CollectionExtractionSpec.from_mapping(best.spec) == spec

        # 其他作用域和场景都不可见。
        assert (
            await store.best_collection_program(
                scope=ExecutionScope("project", "tenant", "other-account"),
                scenario_key="scenario-1",
                url="https://example.com/orders",
            )
            is None
        )
        assert (
            await store.best_collection_program(
                scope=scope,
                scenario_key="scenario-2",
                url="https://example.com/orders",
            )
            is None
        )

        # 同签名重新过门：替换旧程序并重置统计。
        await store.record_collection_program_outcome(
            saved.program_id, success=True, latency_ms=800.0
        )
        replacement = await store.save_collection_program(
            scope=scope,
            scenario_key="scenario-1",
            url="https://example.com/orders?page=3",
            structure_fingerprint=fingerprint,
            spec=spec.to_mapping(),
            summary={"unique_count": 90},
            evidence_id="evidence-2",
        )
        assert replacement.program_id != saved.program_id
        assert replacement.success_count == 0
        with pytest.raises(KeyError):
            await store.get_collection_program(saved.program_id)

        # 连续失败按置信度衰减并最终禁用：0.8 → 0.4 → 0.2 → 0.1 低于阈值。
        for _ in range(3):
            await store.record_collection_program_outcome(
                replacement.program_id, success=False, latency_ms=100.0
            )
        assert (
            await store.best_collection_program(
                scope=scope,
                scenario_key="scenario-1",
                url="https://example.com/orders",
            )
            is None
        )

        with pytest.raises(ValueError, match="敏感"):
            await store.save_collection_program(
                scope=scope,
                scenario_key="scenario-1",
                url="https://example.com/orders",
                structure_fingerprint=fingerprint,
                spec={"row_selector": "[data-token='password=abc']"},
                summary={},
                evidence_id="evidence-3",
            )

    asyncio.run(scenario())


def test_gate_rejects_specs_that_embed_task_inputs(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteUrlMemoryStore(tmp_path / "memory.db")
        runtime = shared_background_memory(store)
        runtime.start()
        spec = _spec(row_selector=".rows[data-user='SECRET-13800138000']")
        task = TaskSpec(
            "gate-inputs",
            "获取全部订单数据",
            "https://example.com/orders",
            ExecutionScope("project"),
            inputs={"phone": "SECRET-13800138000"},
        )
        host = GateHost(RecordingDriver(), runtime, ReplayableExtractor(tmp_path / "exports"))

        assert spec_contains_task_inputs(spec.to_mapping(), task.inputs)
        promoted = await verify_and_promote_collection_program(
            host,
            task,
            spec,
            "https://example.com/orders",
            _complete_result(tmp_path / "exports", spec),
            "evidence-1",
        )

        assert promoted is False
        assert host.events and host.events[0][0] == "collection_program_rejected"
        # 拒绝发生在导航之前，页面状态没有被验证门改变。
        assert host.driver.commands == []
        await runtime.flush()

    asyncio.run(scenario())


def test_gate_rejects_when_entry_probe_keeps_failing(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteUrlMemoryStore(tmp_path / "memory.db")
        runtime = shared_background_memory(store)
        runtime.start()
        spec = _spec()
        task = TaskSpec(
            "gate-probe",
            "获取全部订单数据",
            "https://example.com/orders",
            ExecutionScope("project"),
        )
        extractor = ReplayableExtractor(tmp_path / "exports", probe_row_count=0)
        host = GateHost(RecordingDriver(), runtime, extractor)

        promoted = await verify_and_promote_collection_program(
            host,
            task,
            spec,
            "https://example.com/orders",
            _complete_result(tmp_path / "exports", spec),
            "evidence-1",
        )
        await runtime.flush()

        assert promoted is False
        assert extractor.probe_count >= 1
        assert host.driver.commands[0].kind is ActionKind.NAVIGATE
        assert any(kind == "collection_program_rejected" for kind, _ in host.events)
        assert (
            await store.best_collection_program(
                scope=task.scope,
                scenario_key=scenario_key(task.goal),
                url=task.start_url,
            )
            is None
        )

    asyncio.run(scenario())


def test_first_run_promotes_and_second_run_replays_without_model(tmp_path: Path) -> None:
    """P0 验收：工具库首跑过门晋升，二跑 replay 成功，选择器失效后明确回退。"""

    goal = "获取全部订单数据"
    start_url = "https://example.com/orders"
    scope = ExecutionScope("project", "tenant", "account")

    async def scenario() -> None:
        store = SqliteUrlMemoryStore(tmp_path / "memory.db")
        runtime = shared_background_memory(store)
        runtime.start()
        task = TaskSpec("program-first-run", goal, start_url, scope)

        # 第一跑：经 ToolExecutor 提交规格并过验证门晋升，全程无模型循环。
        first_extractor = ReplayableExtractor(tmp_path / "exports-1")
        first_driver = ListPageDriver(tmp_path / "artifacts-1")
        await first_driver.open(start_url)
        first_executor = ToolExecutor(
            first_driver,
            task,
            structured_extractor=first_extractor,
            memory_runtime=runtime,
        )
        first_observation = await first_driver.observe()
        first_result = await first_executor.execute(
            ModelToolCall("extract-1", "run_structured_extraction", _SPEC_MAPPING),
            first_observation,
        )
        await runtime.flush()

        assert first_result.success
        program = await store.best_collection_program(
            scope=scope,
            scenario_key=scenario_key(goal),
            url=start_url,
        )
        assert program is not None
        assert CollectionExtractionSpec.from_mapping(program.spec) == _spec()

        # 第二跑：同场景零模型重放。
        second_extractor = ReplayableExtractor(tmp_path / "exports-2")
        second_toolkit = BrowserToolkit(
            ListPageDriver(tmp_path / "artifacts-2"),
            TaskSpec("program-second-run", goal, start_url, scope),
            structured_extractor=second_extractor,
            memory_runtime=runtime,
        )
        await second_toolkit.open(start_url)
        second_result = await second_toolkit.replay_collection_program()
        await runtime.flush()

        assert second_result.success
        assert second_extractor.specs == [_spec()]
        assert second_result.data.get("replay") is True
        assert second_result.data.get("去重后总数") == 80
        refreshed = await store.get_collection_program(program.program_id)
        assert refreshed.success_count == 1

        # 第三跑：入口结构失配，重放失败并降权，调用方应回退检查结构。
        third_extractor = ReplayableExtractor(tmp_path / "exports-3", probe_row_count=0)
        third_toolkit = BrowserToolkit(
            ListPageDriver(tmp_path / "artifacts-3"),
            TaskSpec("program-third-run", goal, start_url, scope),
            structured_extractor=third_extractor,
            memory_runtime=runtime,
        )
        await third_toolkit.open(start_url)
        third_result = await third_toolkit.replay_collection_program()
        await runtime.flush()

        assert third_result.success is False
        assert third_extractor.specs == []
        assert third_result.data.get("fallback") == "inspect_collection_structure"
        degraded = await store.get_collection_program(program.program_id)
        assert degraded.failure_count == 1

    asyncio.run(scenario())

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from witty_browser_auto.domain.models import ActionKind, ExecutionScope
from witty_browser_auto.memory.models import MemoryKind, PlanStep
from witty_browser_auto.memory.store import SqliteUrlMemoryStore


def test_concurrent_first_access_shares_one_schema_initialization(tmp_path: Path) -> None:
    class SlowInitializingStore(SqliteUrlMemoryStore):
        initialization_count = 0

        def _initialize_sync(self) -> None:
            self.initialization_count += 1
            time.sleep(0.03)
            super()._initialize_sync()

    async def scenario() -> None:
        store = SlowInitializingStore(tmp_path / "memory.db")
        scope = ExecutionScope("project")

        _, remembered, recalled = await asyncio.gather(
            store.initialize(),
            store.remember(
                scope=scope,
                url="https://example.com/orders",
                kind=MemoryKind.DATA_HINT,
                content={"路线": "先检查结构化数据"},
                page_fingerprint="orders-v1",
                confidence=0.8,
                evidence_id="concurrent-initialize",
            ),
            store.recall(scope=scope, url="https://example.com/orders"),
        )

        assert store.initialization_count == 1
        assert remembered.memory_id
        assert isinstance(recalled, tuple)

    asyncio.run(scenario())


def test_memory_is_isolated_and_exact_url_ranks_first(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteUrlMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        account_a = ExecutionScope("project", "tenant", "account-a")
        account_b = ExecutionScope("project", "tenant", "account-b")
        exact = await store.remember(
            scope=account_a,
            url="https://example.com/orders/10001?page=2",
            kind=MemoryKind.ATTENTION,
            content={"note": "等待订单加载"},
            page_fingerprint="fp-1",
            confidence=0.8,
            evidence_id="evidence-1",
        )
        await store.remember(
            scope=account_a,
            url="https://example.com/orders/20002",
            kind=MemoryKind.RECOVERY,
            content={"note": "刷新页面"},
            page_fingerprint="fp-2",
            confidence=0.9,
            evidence_id="evidence-2",
        )
        await store.remember(
            scope=account_b,
            url="https://example.com/orders/10001?page=2",
            kind=MemoryKind.ATTENTION,
            content={"note": "其他账号"},
            page_fingerprint="fp-1",
            confidence=1.0,
            evidence_id="evidence-3",
        )

        recalled = await store.recall(
            scope=account_a,
            url="https://example.com/orders/10001?page=2",
            page_fingerprint="fp-1",
        )

        assert recalled[0].memory_id == exact.memory_id
        assert all(item.scope.account_id == "account-a" for item in recalled)

    asyncio.run(scenario())


def test_failed_plan_is_disabled_after_three_failures(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteUrlMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        scope = ExecutionScope("project")
        plan = await store.save_plan(
            scope=scope,
            scenario_key="submit-task",
            url="https://example.com/tasks",
            start_fingerprint="fp",
            steps=(
                PlanStep(
                    action_kind=ActionKind.CLICK,
                    target_role="button",
                    target_name="提交",
                    expected_kind="text_contains",
                    expected_value="完成",
                ),
            ),
            evidence_id="evidence-plan",
        )

        for _ in range(3):
            await store.record_plan_outcome(plan.plan_id, success=False, latency_ms=10)

        assert (await store.get_plan(plan.plan_id)).enabled is False
        assert (
            await store.best_plan(
                scope=scope,
                scenario_key="submit-task",
                url="https://example.com/tasks",
                start_fingerprint="fp",
            )
            is None
        )

    asyncio.run(scenario())


def test_fast_plan_is_isolated_by_business_scenario(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteUrlMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        scope = ExecutionScope("project")
        await store.save_plan(
            scope=scope,
            scenario_key="submit-task",
            url="https://example.com/tasks",
            start_fingerprint="fp",
            steps=(
                PlanStep(
                    action_kind=ActionKind.CLICK,
                    target_role="button",
                    target_name="提交",
                    expected_kind="text_contains",
                    expected_value="完成",
                ),
            ),
            evidence_id="evidence-plan",
        )

        assert (
            await store.best_plan(
                scope=scope,
                scenario_key="delete-task",
                url="https://example.com/tasks",
                start_fingerprint="fp",
            )
            is None
        )

    asyncio.run(scenario())


def test_memory_write_requires_evidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteUrlMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        try:
            await store.remember(
                scope=ExecutionScope("project"),
                url="https://example.com",
                kind=MemoryKind.ATTENTION,
                content={"note": "无证据"},
                page_fingerprint="fp",
                confidence=0.5,
                evidence_id="",
            )
        except ValueError as error:
            assert "验证证据" in str(error)
        else:
            raise AssertionError("无证据记忆不应写入")

    asyncio.run(scenario())


def test_site_level_memory_is_shared_across_scopes(tmp_path: Path) -> None:
    """站点级事实必须跨租户和账号复用，否则每个任务都要重新学一遍。"""

    async def scenario() -> None:
        store = SqliteUrlMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        writer = ExecutionScope("project-a", "tenant-a", "account-a")
        reader = ExecutionScope("project-b", "tenant-b", "account-b")
        site_level = await store.remember(
            scope=writer,
            url="https://example.com/order",
            kind=MemoryKind.LOCATOR,
            content={"row_selector": "table tbody tr"},
            page_fingerprint="fp-1",
            confidence=0.95,
            evidence_id="evidence-1",
            site_level=True,
        )
        await store.remember(
            scope=writer,
            url="https://example.com/order",
            kind=MemoryKind.LESSON,
            content={"停止原因": "只属于写入账号的经验"},
            page_fingerprint="fp-1",
            confidence=0.6,
            evidence_id="evidence-2",
        )

        recalled = await store.recall(
            scope=reader,
            url="https://example.com/order",
            page_fingerprint="fp-1",
        )
        recalled_ids = {item.memory_id for item in recalled}

        assert site_level.memory_id in recalled_ids
        assert site_level.scope.project_id == "__global__"
        # 账号相关的教训不得泄露到其他账号。
        assert len(recalled_ids) == 1

    asyncio.run(scenario())

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from witty_browser_auto.domain.models import ExecutionScope
from witty_browser_auto.memory.background import BackgroundMemoryRuntime
from witty_browser_auto.memory.models import MemoryEntry, MemoryKind, VerifiedPlan
from witty_browser_auto.memory.protocols import UrlMemoryStore
from witty_browser_auto.memory.store import SqliteUrlMemoryStore


class BlockingMemoryStore:
    """初始化闸门用于证明主调用方不会等待任何记忆 I/O。"""

    def __init__(self) -> None:
        self.initialization_started = asyncio.Event()
        self.release = asyncio.Event()
        self.recall_count = 0
        self.remember_count = 0

    async def initialize(self) -> None:
        self.initialization_started.set()
        await self.release.wait()

    async def recall(self, **kwargs: Any) -> tuple[MemoryEntry, ...]:
        self.recall_count += 1
        return ()

    async def remember(self, **kwargs: Any) -> MemoryEntry:
        self.remember_count += 1
        now = datetime.now(UTC)
        return MemoryEntry(
            memory_id="background-memory",
            scope=cast(ExecutionScope, kwargs["scope"]),
            normalized_url=str(kwargs["url"]),
            path_template="/tasks",
            site_origin="https://example.com",
            kind=cast(MemoryKind, kwargs["kind"]),
            content=cast(dict[str, Any], kwargs["content"]),
            page_fingerprint=str(kwargs["page_fingerprint"]),
            confidence=float(kwargs["confidence"]),
            evidence_id=str(kwargs["evidence_id"]),
            created_at=now,
            last_verified_at=now,
            success_count=0,
            failure_count=0,
        )

    async def record_memory_outcome(self, memory_id: str, *, success: bool) -> None:
        return None

    async def save_plan(self, **kwargs: Any) -> VerifiedPlan:
        raise AssertionError("阻塞预取不应保存快速计划")

    async def best_plan(self, **kwargs: Any) -> VerifiedPlan | None:
        return None

    async def record_plan_outcome(
        self,
        plan_id: str,
        *,
        success: bool,
        latency_ms: float,
    ) -> None:
        return None

    async def save_collection_program(self, **kwargs: Any) -> Any:
        raise AssertionError("阻塞预取不应保存采集程序")

    async def best_collection_program(self, **kwargs: Any) -> Any:
        return None

    async def record_collection_program_outcome(
        self,
        program_id: str,
        *,
        success: bool,
        latency_ms: float,
    ) -> None:
        return None


def test_background_memory_prefetch_remember_and_flush_do_not_block_caller(
    tmp_path,
) -> None:
    async def scenario() -> None:
        store = BlockingMemoryStore()
        runtime = BackgroundMemoryRuntime(cast(UrlMemoryStore, store))
        scope = ExecutionScope("project")

        assert runtime.prefetch_memories(scope=scope, url="https://example.com/tasks") is True
        await asyncio.wait_for(store.initialization_started.wait(), timeout=1.0)
        assert not store.release.is_set()
        assert runtime.pending_count > 0
        assert store.recall_count == 0

        runtime.remember_later(
            scope=scope,
            url="https://example.com/tasks",
            kind=MemoryKind.ATTENTION,
            content={"note": "后台写入"},
            page_fingerprint="fp-1",
            confidence=0.9,
            evidence_id="bg-evidence",
        )
        assert store.remember_count == 0

        store.release.set()
        assert await runtime.flush(timeout_seconds=1.0)
        assert store.recall_count >= 1
        assert store.remember_count == 1

        # remember_later 会失效缓存；再次预取后应能命中。
        assert runtime.prefetch_memories(scope=scope, url="https://example.com/tasks") is True
        assert await runtime.flush(timeout_seconds=1.0)
        snapshot = runtime.recall_cached(scope=scope, url="https://example.com/tasks")
        assert snapshot.cache_hit is True
        assert snapshot.error_type is None

    asyncio.run(scenario())


def test_sqlite_background_memory_caches_prefetch_results(tmp_path) -> None:
    async def scenario() -> None:
        store = SqliteUrlMemoryStore(tmp_path / "memory.db")
        runtime = BackgroundMemoryRuntime(store)
        scope = ExecutionScope("project", "tenant", "account")
        url = "https://example.com/orders"

        await store.initialize()
        await store.remember(
            scope=scope,
            url=url,
            kind=MemoryKind.DATA_HINT,
            content={"route": "先看列表"},
            page_fingerprint="orders-v1",
            confidence=0.8,
            evidence_id="sqlite-prefetch",
        )

        assert runtime.prefetch_memories(scope=scope, url=url) is True
        assert await runtime.flush(timeout_seconds=1.0)

        snapshot = runtime.recall_cached(scope=scope, url=url)
        assert snapshot.cache_hit is True
        assert len(snapshot.entries) == 1
        assert snapshot.entries[0].content["route"] == "先看列表"

    asyncio.run(scenario())


def test_non_http_page_memory_is_skipped_without_scheduling_background_io() -> None:
    store = BlockingMemoryStore()
    runtime = BackgroundMemoryRuntime(cast(UrlMemoryStore, store))
    scope = ExecutionScope("project")

    snapshot = runtime.recall_cached(scope=scope, url="chrome-error://chromewebdata/")

    assert snapshot.entries == ()
    assert snapshot.refresh_scheduled is False
    assert snapshot.error_type == "UnsupportedMemoryUrl"
    assert runtime.prefetch_memories(scope=scope, url="chrome-error://chromewebdata/") is False
    assert (
        runtime.prefetch_plan(
            scope=scope,
            scenario_key="orders",
            url="chrome-error://chromewebdata/",
            start_fingerprint="error",
        )
        is False
    )
    assert runtime.pending_count == 0

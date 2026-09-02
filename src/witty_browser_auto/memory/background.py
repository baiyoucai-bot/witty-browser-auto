"""不阻塞智能体主循环的 URL 记忆缓存与后台写回。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from weakref import WeakKeyDictionary

from witty_browser_auto.domain.errors import ConfigurationError
from witty_browser_auto.domain.models import ExecutionScope
from witty_browser_auto.memory.models import (
    CollectionProgram,
    MemoryEntry,
    MemoryKind,
    PlanStep,
    VerifiedPlan,
)
from witty_browser_auto.memory.protocols import UrlMemoryStore
from witty_browser_auto.memory.url import normalize_url

logger = logging.getLogger(__name__)

_MEMORY_PREFETCH_LIMIT = 20
_CACHE_REFRESH_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    entries: tuple[MemoryEntry, ...]
    cache_hit: bool
    refresh_scheduled: bool
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    plan: VerifiedPlan | None
    cache_hit: bool
    refresh_scheduled: bool


@dataclass(frozen=True, slots=True)
class ProgramSnapshot:
    program: CollectionProgram | None
    cache_hit: bool
    refresh_scheduled: bool


class BackgroundMemoryRuntime:
    """主循环只读取内存快照，SQLite I/O 全部在独立 asyncio 任务中完成。"""

    def __init__(self, store: UrlMemoryStore) -> None:
        self.store = store
        self._initialization: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._reads: dict[tuple[object, ...], asyncio.Task[Any]] = {}
        self._memory_cache: dict[tuple[str, str], tuple[MemoryEntry, ...]] = {}
        self._memory_refreshed_at: dict[tuple[str, str], float] = {}
        self._memory_errors: dict[tuple[str, str], str] = {}
        self._plan_cache: dict[tuple[str, str, str, str], VerifiedPlan | None] = {}
        self._plan_refreshed_at: dict[tuple[str, str, str, str], float] = {}
        self._program_cache: dict[tuple[str, str, str], CollectionProgram | None] = {}
        self._program_refreshed_at: dict[tuple[str, str, str], float] = {}
        self._closed = False

    @property
    def pending_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        if self._closed or self._initialization is not None:
            return
        self._initialization = self._track(
            asyncio.create_task(self.store.initialize(), name="witty-memory-initialize")
        )

    def recall_cached(
        self,
        *,
        scope: ExecutionScope,
        url: str,
        page_fingerprint: str = "",
        limit: int = 12,
    ) -> MemorySnapshot:
        key = self._optional_memory_key(scope, url)
        if key is None:
            return MemorySnapshot((), False, False, "UnsupportedMemoryUrl")
        cached = self._memory_cache.get(key)
        scheduled = self._prefetch_memories(
            key,
            scope=scope,
            url=url,
            page_fingerprint=page_fingerprint,
        )
        return MemorySnapshot(
            entries=tuple((cached or ())[: max(1, limit)]),
            cache_hit=cached is not None,
            refresh_scheduled=scheduled,
            error_type=self._memory_errors.get(key),
        )

    def prefetch_memories(
        self,
        *,
        scope: ExecutionScope,
        url: str,
        page_fingerprint: str = "",
    ) -> bool:
        key = self._optional_memory_key(scope, url)
        if key is None:
            return False
        return self._prefetch_memories(
            key,
            scope=scope,
            url=url,
            page_fingerprint=page_fingerprint,
        )

    def best_plan_cached(
        self,
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
        start_fingerprint: str,
    ) -> PlanSnapshot:
        key = self._optional_plan_key(scope, scenario_key, url, start_fingerprint)
        if key is None:
            return PlanSnapshot(None, False, False)
        cached = self._plan_cache.get(key)
        cache_hit = key in self._plan_cache
        scheduled = self._prefetch_plan(
            key,
            scope=scope,
            scenario_key=scenario_key,
            url=url,
            start_fingerprint=start_fingerprint,
        )
        return PlanSnapshot(cached, cache_hit, scheduled)

    def prefetch_plan(
        self,
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
        start_fingerprint: str,
    ) -> bool:
        key = self._optional_plan_key(scope, scenario_key, url, start_fingerprint)
        if key is None:
            return False
        return self._prefetch_plan(
            key,
            scope=scope,
            scenario_key=scenario_key,
            url=url,
            start_fingerprint=start_fingerprint,
        )

    def best_collection_program_cached(
        self,
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
    ) -> ProgramSnapshot:
        key = self._optional_program_key(scope, scenario_key, url)
        if key is None:
            return ProgramSnapshot(None, False, False)
        cached = self._program_cache.get(key)
        cache_hit = key in self._program_cache
        scheduled = self._prefetch_program(key, scope=scope, scenario_key=scenario_key, url=url)
        return ProgramSnapshot(cached, cache_hit, scheduled)

    def prefetch_collection_program(
        self,
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
    ) -> bool:
        key = self._optional_program_key(scope, scenario_key, url)
        if key is None:
            return False
        return self._prefetch_program(key, scope=scope, scenario_key=scenario_key, url=url)

    def remember_later(
        self,
        *,
        scope: ExecutionScope,
        url: str,
        kind: MemoryKind,
        content: dict[str, Any],
        page_fingerprint: str,
        confidence: float,
        evidence_id: str,
    ) -> None:
        if self._optional_memory_key(scope, url) is None:
            return

        async def write() -> None:
            await self._ensure_initialized()
            await self.store.remember(
                scope=scope,
                url=url,
                kind=kind,
                content=content,
                page_fingerprint=page_fingerprint,
                confidence=confidence,
                evidence_id=evidence_id,
            )
            self._invalidate_memories(scope, url)

        self._spawn(write(), name="witty-memory-remember")

    def record_memory_outcome_later(self, memory_id: str, *, success: bool) -> None:
        async def write() -> None:
            await self._ensure_initialized()
            await self.store.record_memory_outcome(memory_id, success=success)
            self._memory_cache.clear()
            self._memory_refreshed_at.clear()

        self._spawn(write(), name="witty-memory-outcome")

    def save_plan_later(
        self,
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
        start_fingerprint: str,
        steps: tuple[PlanStep, ...],
        evidence_id: str,
        confidence: float = 0.8,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        key = self._optional_plan_key(scope, scenario_key, url, start_fingerprint)
        if key is None:
            return

        async def write() -> None:
            await self._ensure_initialized()
            plan = await self.store.save_plan(
                scope=scope,
                scenario_key=scenario_key,
                url=url,
                start_fingerprint=start_fingerprint,
                steps=steps,
                evidence_id=evidence_id,
                confidence=confidence,
                metadata=metadata,
            )
            self._plan_cache[key] = plan
            self._plan_refreshed_at[key] = monotonic()

        self._spawn(write(), name="witty-memory-save-plan")

    def record_plan_outcome_later(
        self,
        plan_id: str,
        *,
        success: bool,
        latency_ms: float,
    ) -> None:
        async def write() -> None:
            await self._ensure_initialized()
            await self.store.record_plan_outcome(
                plan_id,
                success=success,
                latency_ms=latency_ms,
            )
            self._plan_cache.clear()
            self._plan_refreshed_at.clear()

        self._spawn(write(), name="witty-memory-plan-outcome")

    def save_collection_program_later(
        self,
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
        structure_fingerprint: str,
        spec: dict[str, Any],
        summary: dict[str, Any],
        evidence_id: str,
        confidence: float = 0.8,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        key = self._optional_program_key(scope, scenario_key, url)
        if key is None:
            return

        async def write() -> None:
            await self._ensure_initialized()
            program = await self.store.save_collection_program(
                scope=scope,
                scenario_key=scenario_key,
                url=url,
                structure_fingerprint=structure_fingerprint,
                spec=spec,
                summary=summary,
                evidence_id=evidence_id,
                confidence=confidence,
                metadata=metadata,
            )
            self._program_cache[key] = program
            self._program_refreshed_at[key] = monotonic()

        self._spawn(write(), name="witty-memory-save-program")

    def record_collection_program_outcome_later(
        self,
        program_id: str,
        *,
        success: bool,
        latency_ms: float,
    ) -> None:
        async def write() -> None:
            await self._ensure_initialized()
            await self.store.record_collection_program_outcome(
                program_id,
                success=success,
                latency_ms=latency_ms,
            )
            self._program_cache.clear()
            self._program_refreshed_at.clear()

        self._spawn(write(), name="witty-memory-program-outcome")

    async def await_best_collection_program(
        self,
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
    ) -> CollectionProgram | None:
        """等待初始化完成后同步查询最佳采集程序，并刷新内存缓存。"""

        await self._ensure_initialized()
        program = await self.store.best_collection_program(
            scope=scope,
            scenario_key=scenario_key,
            url=url,
        )
        key = self._optional_program_key(scope, scenario_key, url)
        if key is not None:
            self._program_cache[key] = program
            self._program_refreshed_at[key] = monotonic()
        return program

    async def flush(self, *, timeout_seconds: float = 5.0) -> bool:
        pending = tuple(task for task in self._tasks if not task.done())
        if not pending:
            return True
        _, unfinished = await asyncio.wait(pending, timeout=max(0.0, timeout_seconds))
        if unfinished:
            logger.warning("后台记忆任务未在关闭窗口内完成", extra={"count": len(unfinished)})
            return False
        return True

    async def close(self, *, timeout_seconds: float = 5.0) -> bool:
        flushed = await self.flush(timeout_seconds=timeout_seconds)
        self._closed = True
        return flushed

    def _prefetch_memories(
        self,
        key: tuple[str, str],
        *,
        scope: ExecutionScope,
        url: str,
        page_fingerprint: str,
    ) -> bool:
        read_key = ("memories", *key)
        if not self._refresh_due(read_key, self._memory_refreshed_at.get(key)):
            return False

        async def read() -> None:
            await self._ensure_initialized()
            try:
                entries = await self.store.recall(
                    scope=scope,
                    url=url,
                    page_fingerprint=page_fingerprint,
                    limit=_MEMORY_PREFETCH_LIMIT,
                )
            except Exception as exc:
                self._memory_errors[key] = type(exc).__name__
                raise
            self._memory_cache[key] = entries
            self._memory_refreshed_at[key] = monotonic()
            self._memory_errors.pop(key, None)

        return self._spawn_read(read_key, read(), name="witty-memory-recall")

    def _prefetch_plan(
        self,
        key: tuple[str, str, str, str],
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
        start_fingerprint: str,
    ) -> bool:
        read_key = ("plan", *key)
        if not self._refresh_due(read_key, self._plan_refreshed_at.get(key)):
            return False

        async def read() -> None:
            await self._ensure_initialized()
            plan = await self.store.best_plan(
                scope=scope,
                scenario_key=scenario_key,
                url=url,
                start_fingerprint=start_fingerprint,
            )
            self._plan_cache[key] = plan
            self._plan_refreshed_at[key] = monotonic()

        return self._spawn_read(read_key, read(), name="witty-memory-best-plan")

    def _prefetch_program(
        self,
        key: tuple[str, str, str],
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
    ) -> bool:
        read_key = ("program", *key)
        if not self._refresh_due(read_key, self._program_refreshed_at.get(key)):
            return False

        async def read() -> None:
            await self._ensure_initialized()
            program = await self.store.best_collection_program(
                scope=scope,
                scenario_key=scenario_key,
                url=url,
            )
            self._program_cache[key] = program
            self._program_refreshed_at[key] = monotonic()

        return self._spawn_read(read_key, read(), name="witty-memory-best-program")

    def _refresh_due(self, read_key: tuple[object, ...], refreshed_at: float | None) -> bool:
        task = self._reads.get(read_key)
        if task is not None and not task.done():
            return False
        return refreshed_at is None or monotonic() - refreshed_at >= _CACHE_REFRESH_SECONDS

    def _spawn_read(
        self,
        key: tuple[object, ...],
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str,
    ) -> bool:
        if self._closed:
            coroutine.close()
            return False
        task = self._track(asyncio.create_task(coroutine, name=name))
        self._reads[key] = task

        def clear_read(done: asyncio.Task[Any]) -> None:
            if self._reads.get(key) is done:
                self._reads.pop(key, None)

        task.add_done_callback(clear_read)
        return True

    def _spawn(self, coroutine: Coroutine[Any, Any, None], *, name: str) -> None:
        if self._closed:
            coroutine.close()
            logger.warning("后台记忆运行时已关闭，忽略新的记忆任务", extra={"task_name": name})
            return
        self._track(asyncio.create_task(coroutine, name=name))

    def _track(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        self._tasks.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                logger.warning(
                    "后台记忆任务失败",
                    extra={"task_name": done.get_name(), "error_type": type(error).__name__},
                )

        task.add_done_callback(completed)
        return task

    async def _ensure_initialized(self) -> None:
        self.start()
        assert self._initialization is not None
        await self._initialization

    @staticmethod
    def _memory_key(scope: ExecutionScope, url: str) -> tuple[str, str]:
        return scope.memory_key, normalize_url(url).exact

    @classmethod
    def _optional_memory_key(
        cls,
        scope: ExecutionScope,
        url: str,
    ) -> tuple[str, str] | None:
        try:
            return cls._memory_key(scope, url)
        except ConfigurationError:
            logger.debug("跳过非 HTTP 页面记忆，不影响主任务执行")
            return None

    @staticmethod
    def _plan_key(
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
        start_fingerprint: str,
    ) -> tuple[str, str, str, str]:
        return scope.memory_key, scenario_key, normalize_url(url).exact, start_fingerprint

    @classmethod
    def _optional_plan_key(
        cls,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
        start_fingerprint: str,
    ) -> tuple[str, str, str, str] | None:
        try:
            return cls._plan_key(scope, scenario_key, url, start_fingerprint)
        except ConfigurationError:
            logger.debug("跳过非 HTTP 页面计划记忆，不影响主任务执行")
            return None

    @classmethod
    def _optional_program_key(
        cls,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
    ) -> tuple[str, str, str] | None:
        try:
            return scope.memory_key, scenario_key, normalize_url(url).exact
        except ConfigurationError:
            logger.debug("跳过非 HTTP 页面采集程序记忆，不影响主任务执行")
            return None

    def _invalidate_memories(self, scope: ExecutionScope, url: str) -> None:
        key = self._memory_key(scope, url)
        self._memory_cache.pop(key, None)
        self._memory_refreshed_at.pop(key, None)
        self._memory_errors.pop(key, None)


_RUNTIMES: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, BackgroundMemoryRuntime],
] = WeakKeyDictionary()


def shared_background_memory(store: UrlMemoryStore) -> BackgroundMemoryRuntime:
    """同一事件循环和 SQLite 文件共享缓存及后台写队列。"""

    loop = asyncio.get_running_loop()
    database_path = getattr(store, "database_path", None)
    identity = (
        str(Path(database_path).expanduser().resolve())
        if database_path is not None
        else f"object:{id(store)}"
    )
    runtimes = _RUNTIMES.setdefault(loop, {})
    runtime = runtimes.get(identity)
    if runtime is None or runtime.closed:
        runtime = BackgroundMemoryRuntime(store)
        runtimes[identity] = runtime
    return runtime

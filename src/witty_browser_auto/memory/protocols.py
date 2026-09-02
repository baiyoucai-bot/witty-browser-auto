"""智能体依赖的记忆协议，隔离具体数据库实现。"""

from __future__ import annotations

from typing import Any, Protocol

from witty_browser_auto.domain.models import ExecutionScope
from witty_browser_auto.memory.models import (
    CollectionProgram,
    MemoryEntry,
    MemoryKind,
    PlanStep,
    VerifiedPlan,
)


class UrlMemoryStore(Protocol):
    async def initialize(self) -> None: ...

    async def remember(
        self,
        *,
        scope: ExecutionScope,
        url: str,
        kind: MemoryKind,
        content: dict[str, Any],
        page_fingerprint: str,
        confidence: float,
        evidence_id: str,
    ) -> MemoryEntry: ...

    async def recall(
        self,
        *,
        scope: ExecutionScope,
        url: str,
        page_fingerprint: str = "",
        limit: int = 12,
    ) -> tuple[MemoryEntry, ...]: ...

    async def record_memory_outcome(self, memory_id: str, *, success: bool) -> None: ...

    async def save_plan(
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
    ) -> VerifiedPlan: ...

    async def best_plan(
        self,
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
        start_fingerprint: str,
    ) -> VerifiedPlan | None: ...

    async def record_plan_outcome(
        self,
        plan_id: str,
        *,
        success: bool,
        latency_ms: float,
    ) -> None: ...

    async def save_collection_program(
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
    ) -> CollectionProgram: ...

    async def best_collection_program(
        self,
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
    ) -> CollectionProgram | None: ...

    async def record_collection_program_outcome(
        self,
        program_id: str,
        *,
        success: bool,
        latency_ms: float,
    ) -> None: ...

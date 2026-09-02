"""SQLite 单机 URL 记忆实现。"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from witty_browser_auto.domain.models import ExecutionScope
from witty_browser_auto.memory.models import (
    GLOBAL_SCOPE,
    CollectionProgram,
    MemoryEntry,
    MemoryKind,
    PlanStep,
    VerifiedPlan,
)
from witty_browser_auto.memory.url import NormalizedUrl, normalize_url
from witty_browser_auto.security.redaction import redact


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SqliteUrlMemoryStore:
    """每次操作使用短连接，避免 SQLite 连接跨线程复用。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._initialization_lock = asyncio.Lock()
        self._initialized = False
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

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
        site_level: bool = False,
    ) -> MemoryEntry:
        """写入记忆。`site_level` 为真时写入站点级全局作用域，供其他租户和账号复用。"""

        if not evidence_id.strip():
            raise ValueError("写入 URL 记忆必须提供验证证据 ID")
        await self.initialize()
        if site_level:
            scope = GLOBAL_SCOPE
        normalized = normalize_url(url)
        memory_id = uuid.uuid4().hex
        timestamp = _utc_iso()
        safe_content = redact(content)
        async with self._write_lock:
            await asyncio.to_thread(
                self._insert_memory_sync,
                memory_id,
                scope,
                normalized,
                kind,
                safe_content,
                page_fingerprint,
                min(1.0, max(0.0, confidence)),
                evidence_id,
                timestamp,
            )
        return await self.get_memory(memory_id)

    async def get_memory(self, memory_id: str) -> MemoryEntry:
        await self.initialize()
        row = await asyncio.to_thread(self._get_one_sync, "memory_entries", memory_id)
        if row is None:
            raise KeyError(f"URL 记忆不存在：{memory_id}")
        return self._memory_from_row(row)

    async def recall(
        self,
        *,
        scope: ExecutionScope,
        url: str,
        page_fingerprint: str = "",
        limit: int = 12,
    ) -> tuple[MemoryEntry, ...]:
        normalized = normalize_url(url)
        await self.initialize()
        rows = await asyncio.to_thread(self._recall_sync, scope, normalized)
        entries = [self._memory_from_row(row) for row in rows]
        ranked = [
            self._with_score(
                entry,
                self._memory_score(entry, normalized, page_fingerprint),
            )
            for entry in entries
        ]
        ranked.sort(key=lambda item: item.score, reverse=True)
        return tuple(ranked[: max(1, limit)])

    async def record_memory_outcome(self, memory_id: str, *, success: bool) -> None:
        await self.initialize()
        async with self._write_lock:
            await asyncio.to_thread(self._record_memory_outcome_sync, memory_id, success)

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
    ) -> VerifiedPlan:
        if not scenario_key.strip():
            raise ValueError("保存快速计划必须提供业务场景键")
        if not evidence_id.strip():
            raise ValueError("保存快速计划必须提供验证证据 ID")
        if not steps:
            raise ValueError("快速计划至少需要一个动作")
        for step in steps:
            if step.static_value and self._looks_sensitive(step.static_value):
                raise ValueError("快速计划不能保存疑似敏感静态值")
        await self.initialize()
        normalized = normalize_url(url)
        plan_id = uuid.uuid4().hex
        timestamp = _utc_iso()
        steps_json = json.dumps([step.to_dict() for step in steps], ensure_ascii=False)
        metadata_json = json.dumps(redact(metadata or {}), ensure_ascii=False)
        async with self._write_lock:
            await asyncio.to_thread(
                self._insert_plan_sync,
                plan_id,
                scope,
                scenario_key,
                normalized,
                start_fingerprint,
                steps_json,
                min(1.0, max(0.0, confidence)),
                evidence_id,
                metadata_json,
                timestamp,
            )
        plan = await self.get_plan(plan_id)
        return plan

    async def get_plan(self, plan_id: str) -> VerifiedPlan:
        await self.initialize()
        row = await asyncio.to_thread(self._get_one_sync, "verified_plans", plan_id)
        if row is None:
            raise KeyError(f"快速计划不存在：{plan_id}")
        return self._plan_from_row(row)

    async def best_plan(
        self,
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
        start_fingerprint: str,
    ) -> VerifiedPlan | None:
        if not scenario_key.strip():
            return None
        normalized = normalize_url(url)
        await self.initialize()
        rows = await asyncio.to_thread(self._plans_sync, scope, scenario_key, normalized)
        plans = [self._plan_from_row(row) for row in rows]
        matching = [
            plan
            for plan in plans
            if plan.enabled
            and plan.start_fingerprint == start_fingerprint
            and (
                plan.normalized_url == normalized.exact
                or plan.path_template == normalized.path_template
            )
        ]
        if not matching:
            return None
        matching.sort(key=self._plan_score, reverse=True)
        return matching[0]

    async def record_plan_outcome(
        self,
        plan_id: str,
        *,
        success: bool,
        latency_ms: float,
    ) -> None:
        await self.initialize()
        async with self._write_lock:
            await asyncio.to_thread(
                self._record_plan_outcome_sync,
                plan_id,
                success,
                max(0.0, latency_ms),
            )

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
    ) -> CollectionProgram:
        """保存通过验证门的采集程序；同签名 (场景+路径+结构指纹) 替换旧程序。"""

        if not scenario_key.strip():
            raise ValueError("保存采集程序必须提供业务场景键")
        if not structure_fingerprint.strip():
            raise ValueError("保存采集程序必须提供结构指纹")
        if not evidence_id.strip():
            raise ValueError("保存采集程序必须提供验证证据 ID")
        if not spec:
            raise ValueError("保存采集程序必须提供采集规格")
        spec_json = json.dumps(spec, ensure_ascii=False, sort_keys=True)
        if self._looks_sensitive(spec_json):
            raise ValueError("采集程序规格包含疑似敏感值，拒绝持久化")
        await self.initialize()
        normalized = normalize_url(url)
        program_id = uuid.uuid4().hex
        timestamp = _utc_iso()
        summary_json = json.dumps(redact(summary), ensure_ascii=False)
        metadata_json = json.dumps(redact(metadata or {}), ensure_ascii=False)
        async with self._write_lock:
            await asyncio.to_thread(
                self._upsert_program_sync,
                program_id,
                scope,
                scenario_key,
                normalized,
                structure_fingerprint,
                spec_json,
                summary_json,
                min(1.0, max(0.0, confidence)),
                evidence_id,
                metadata_json,
                timestamp,
            )
        return await self.get_collection_program(program_id)

    async def get_collection_program(self, program_id: str) -> CollectionProgram:
        await self.initialize()
        row = await asyncio.to_thread(self._get_one_sync, "collection_programs", program_id)
        if row is None:
            raise KeyError(f"采集程序不存在：{program_id}")
        return self._program_from_row(row)

    async def best_collection_program(
        self,
        *,
        scope: ExecutionScope,
        scenario_key: str,
        url: str,
    ) -> CollectionProgram | None:
        if not scenario_key.strip():
            return None
        normalized = normalize_url(url)
        await self.initialize()
        rows = await asyncio.to_thread(self._programs_sync, scope, scenario_key, normalized)
        programs = [self._program_from_row(row) for row in rows]
        matching = [
            program
            for program in programs
            if program.enabled
            and (
                program.normalized_url == normalized.exact
                or program.path_template == normalized.path_template
            )
        ]
        if not matching:
            return None
        matching.sort(key=self._program_score, reverse=True)
        return matching[0]

    async def record_collection_program_outcome(
        self,
        program_id: str,
        *,
        success: bool,
        latency_ms: float,
    ) -> None:
        await self.initialize()
        async with self._write_lock:
            await asyncio.to_thread(
                self._record_program_outcome_sync,
                program_id,
                success,
                max(0.0, latency_ms),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_sync(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_entries (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    site_origin TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    path_template TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    page_fingerprint TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    evidence_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_verified_at TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    invalidated INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_memory_scope_origin
                ON memory_entries(project_id, tenant_id, account_id, site_origin);

                CREATE TABLE IF NOT EXISTS verified_plans (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    scenario_key TEXT NOT NULL,
                    site_origin TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    path_template TEXT NOT NULL,
                    start_fingerprint TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    evidence_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_verified_at TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    average_latency_ms REAL NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                """
            )

            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(verified_plans)")
            }
            if "scenario_key" not in columns:
                # 旧数据库中的计划没有业务场景证据，迁移后保持不可命中。
                connection.execute(
                    "ALTER TABLE verified_plans ADD COLUMN scenario_key TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_plan_scope_scenario_origin
                ON verified_plans(
                    project_id, tenant_id, account_id, scenario_key, site_origin
                )
                """
            )

            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS collection_programs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    scenario_key TEXT NOT NULL,
                    site_origin TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    path_template TEXT NOT NULL,
                    structure_fingerprint TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    evidence_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_verified_at TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    average_latency_ms REAL NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_program_scope_scenario_origin
                ON collection_programs(
                    project_id, tenant_id, account_id, scenario_key, site_origin
                );
                """
            )

    def _insert_memory_sync(
        self,
        memory_id: str,
        scope: ExecutionScope,
        normalized: NormalizedUrl,
        kind: MemoryKind,
        content: Any,
        fingerprint: str,
        confidence: float,
        evidence_id: str,
        timestamp: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_entries (
                    id, project_id, tenant_id, account_id, site_origin, normalized_url,
                    path_template, kind, content_json, page_fingerprint, confidence,
                    evidence_id, created_at, last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    scope.project_id,
                    scope.tenant_id,
                    scope.account_id,
                    normalized.origin,
                    normalized.exact,
                    normalized.path_template,
                    kind.value,
                    json.dumps(content, ensure_ascii=False),
                    fingerprint,
                    confidence,
                    evidence_id,
                    timestamp,
                    timestamp,
                ),
            )

    def _insert_plan_sync(
        self,
        plan_id: str,
        scope: ExecutionScope,
        scenario_key: str,
        normalized: NormalizedUrl,
        fingerprint: str,
        steps_json: str,
        confidence: float,
        evidence_id: str,
        metadata_json: str,
        timestamp: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO verified_plans (
                    id, project_id, tenant_id, account_id, scenario_key,
                    site_origin, normalized_url,
                    path_template, start_fingerprint, steps_json, confidence, evidence_id,
                    metadata_json, created_at, last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    scope.project_id,
                    scope.tenant_id,
                    scope.account_id,
                    scenario_key,
                    normalized.origin,
                    normalized.exact,
                    normalized.path_template,
                    fingerprint,
                    steps_json,
                    confidence,
                    evidence_id,
                    metadata_json,
                    timestamp,
                    timestamp,
                ),
            )

    def _upsert_program_sync(
        self,
        program_id: str,
        scope: ExecutionScope,
        scenario_key: str,
        normalized: NormalizedUrl,
        structure_fingerprint: str,
        spec_json: str,
        summary_json: str,
        confidence: float,
        evidence_id: str,
        metadata_json: str,
        timestamp: str,
    ) -> None:
        # 同签名 (作用域+场景+路径模板+结构指纹) 只保留最新一次过门的程序，
        # 统计从零重新累计：旧统计描述的是旧规格，不能继承到新规格上。
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM collection_programs
                WHERE project_id = ? AND tenant_id = ? AND account_id = ?
                  AND scenario_key = ? AND site_origin = ? AND path_template = ?
                  AND structure_fingerprint = ?
                """,
                (
                    scope.project_id,
                    scope.tenant_id,
                    scope.account_id,
                    scenario_key,
                    normalized.origin,
                    normalized.path_template,
                    structure_fingerprint,
                ),
            )
            connection.execute(
                """
                INSERT INTO collection_programs (
                    id, project_id, tenant_id, account_id, scenario_key,
                    site_origin, normalized_url, path_template, structure_fingerprint,
                    spec_json, summary_json, confidence, evidence_id, metadata_json,
                    created_at, last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    program_id,
                    scope.project_id,
                    scope.tenant_id,
                    scope.account_id,
                    scenario_key,
                    normalized.origin,
                    normalized.exact,
                    normalized.path_template,
                    structure_fingerprint,
                    spec_json,
                    summary_json,
                    confidence,
                    evidence_id,
                    metadata_json,
                    timestamp,
                    timestamp,
                ),
            )

    def _programs_sync(
        self,
        scope: ExecutionScope,
        scenario_key: str,
        normalized: NormalizedUrl,
    ) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM collection_programs
                    WHERE project_id = ? AND tenant_id = ? AND account_id = ?
                      AND scenario_key = ? AND site_origin = ? AND enabled = 1
                    LIMIT 100
                    """,
                    (
                        scope.project_id,
                        scope.tenant_id,
                        scope.account_id,
                        scenario_key,
                        normalized.origin,
                    ),
                ).fetchall()
            )

    def _record_program_outcome_sync(
        self,
        program_id: str,
        success: bool,
        latency_ms: float,
    ) -> None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT confidence, success_count, failure_count, average_latency_ms
                FROM collection_programs WHERE id = ?
                """,
                (program_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"采集程序不存在：{program_id}")
            successes = int(row["success_count"])
            failures = int(row["failure_count"])
            confidence = float(row["confidence"])
            average = float(row["average_latency_ms"])
            if success:
                new_successes = successes + 1
                average = ((average * successes) + latency_ms) / new_successes
                confidence = min(1.0, confidence + 0.05)
                connection.execute(
                    """
                    UPDATE collection_programs
                    SET success_count = ?, confidence = ?, average_latency_ms = ?,
                        last_verified_at = ? WHERE id = ?
                    """,
                    (new_successes, confidence, average, _utc_iso(), program_id),
                )
            else:
                failures += 1
                confidence = max(0.0, confidence * 0.5)
                connection.execute(
                    """
                    UPDATE collection_programs
                    SET failure_count = ?, confidence = ?, enabled = ?,
                        last_verified_at = ? WHERE id = ?
                    """,
                    (
                        failures,
                        confidence,
                        int(failures < 3 and confidence >= 0.2),
                        _utc_iso(),
                        program_id,
                    ),
                )

    def _get_one_sync(self, table: str, item_id: str) -> sqlite3.Row | None:
        if table not in {"memory_entries", "verified_plans", "collection_programs"}:
            raise ValueError("不允许的记忆数据表")
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {table} WHERE id = ?",
                (item_id,),
            ).fetchone()
            return cast(sqlite3.Row | None, row)

    def _recall_sync(
        self,
        scope: ExecutionScope,
        normalized: NormalizedUrl,
    ) -> list[sqlite3.Row]:
        # 同时取任务作用域记忆和站点级全局记忆：前者是账号相关的经验，后者是站点事实。
        with self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM memory_entries
                    WHERE site_origin = ? AND invalidated = 0
                      AND (
                        (project_id = ? AND tenant_id = ? AND account_id = ?)
                        OR (project_id = ? AND tenant_id = ? AND account_id = ?)
                      )
                    LIMIT 400
                    """,
                    (
                        normalized.origin,
                        scope.project_id,
                        scope.tenant_id,
                        scope.account_id,
                        GLOBAL_SCOPE.project_id,
                        GLOBAL_SCOPE.tenant_id,
                        GLOBAL_SCOPE.account_id,
                    ),
                ).fetchall()
            )

    def _plans_sync(
        self,
        scope: ExecutionScope,
        scenario_key: str,
        normalized: NormalizedUrl,
    ) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM verified_plans
                    WHERE project_id = ? AND tenant_id = ? AND account_id = ?
                      AND scenario_key = ? AND site_origin = ? AND enabled = 1
                    LIMIT 100
                    """,
                    (
                        scope.project_id,
                        scope.tenant_id,
                        scope.account_id,
                        scenario_key,
                        normalized.origin,
                    ),
                ).fetchall()
            )

    def _record_memory_outcome_sync(self, memory_id: str, success: bool) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT confidence, failure_count FROM memory_entries WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"URL 记忆不存在：{memory_id}")
            confidence = float(row["confidence"])
            if success:
                confidence = min(1.0, confidence + 0.03)
                connection.execute(
                    """
                    UPDATE memory_entries
                    SET success_count = success_count + 1, confidence = ?, last_verified_at = ?
                    WHERE id = ?
                    """,
                    (confidence, _utc_iso(), memory_id),
                )
            else:
                confidence = max(0.0, confidence * 0.65)
                failures = int(row["failure_count"]) + 1
                connection.execute(
                    """
                    UPDATE memory_entries
                    SET failure_count = failure_count + 1, confidence = ?,
                        invalidated = ?, last_verified_at = ?
                    WHERE id = ?
                    """,
                    (confidence, int(failures >= 3 or confidence < 0.2), _utc_iso(), memory_id),
                )

    def _record_plan_outcome_sync(self, plan_id: str, success: bool, latency_ms: float) -> None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT confidence, success_count, failure_count, average_latency_ms
                FROM verified_plans WHERE id = ?
                """,
                (plan_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"快速计划不存在：{plan_id}")
            successes = int(row["success_count"])
            failures = int(row["failure_count"])
            confidence = float(row["confidence"])
            average = float(row["average_latency_ms"])
            if success:
                new_successes = successes + 1
                average = ((average * successes) + latency_ms) / new_successes
                confidence = min(1.0, confidence + 0.05)
                connection.execute(
                    """
                    UPDATE verified_plans
                    SET success_count = ?, confidence = ?, average_latency_ms = ?,
                        last_verified_at = ? WHERE id = ?
                    """,
                    (new_successes, confidence, average, _utc_iso(), plan_id),
                )
            else:
                failures += 1
                confidence = max(0.0, confidence * 0.5)
                connection.execute(
                    """
                    UPDATE verified_plans
                    SET failure_count = ?, confidence = ?, enabled = ?,
                        last_verified_at = ? WHERE id = ?
                    """,
                    (
                        failures,
                        confidence,
                        int(failures < 3 and confidence >= 0.2),
                        _utc_iso(),
                        plan_id,
                    ),
                )

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            memory_id=str(row["id"]),
            scope=ExecutionScope(
                project_id=str(row["project_id"]),
                tenant_id=str(row["tenant_id"]),
                account_id=str(row["account_id"]),
            ),
            normalized_url=str(row["normalized_url"]),
            path_template=str(row["path_template"]),
            site_origin=str(row["site_origin"]),
            kind=MemoryKind(str(row["kind"])),
            content=json.loads(str(row["content_json"])),
            page_fingerprint=str(row["page_fingerprint"]),
            confidence=float(row["confidence"]),
            evidence_id=str(row["evidence_id"]),
            created_at=_parse_time(str(row["created_at"])),
            last_verified_at=_parse_time(str(row["last_verified_at"])),
            success_count=int(row["success_count"]),
            failure_count=int(row["failure_count"]),
        )

    @staticmethod
    def _plan_from_row(row: sqlite3.Row) -> VerifiedPlan:
        steps_data = json.loads(str(row["steps_json"]))
        return VerifiedPlan(
            plan_id=str(row["id"]),
            scope=ExecutionScope(
                project_id=str(row["project_id"]),
                tenant_id=str(row["tenant_id"]),
                account_id=str(row["account_id"]),
            ),
            scenario_key=str(row["scenario_key"]),
            normalized_url=str(row["normalized_url"]),
            path_template=str(row["path_template"]),
            site_origin=str(row["site_origin"]),
            start_fingerprint=str(row["start_fingerprint"]),
            steps=tuple(PlanStep.from_dict(item) for item in steps_data),
            confidence=float(row["confidence"]),
            evidence_id=str(row["evidence_id"]),
            created_at=_parse_time(str(row["created_at"])),
            last_verified_at=_parse_time(str(row["last_verified_at"])),
            success_count=int(row["success_count"]),
            failure_count=int(row["failure_count"]),
            average_latency_ms=float(row["average_latency_ms"]),
            enabled=bool(row["enabled"]),
            metadata=json.loads(str(row["metadata_json"])),
        )

    @staticmethod
    def _memory_score(
        entry: MemoryEntry,
        normalized: NormalizedUrl,
        page_fingerprint: str,
    ) -> float:
        url_score = 4.0 if entry.normalized_url == normalized.exact else 0.0
        path_score = 2.0 if entry.path_template == normalized.path_template else 0.0
        fingerprint_score = (
            2.0 if page_fingerprint and entry.page_fingerprint == page_fingerprint else 0.0
        )
        outcome_total = entry.success_count + entry.failure_count
        outcome_score = entry.success_count / outcome_total if outcome_total else 0.5
        age_days = max(
            0.0,
            (datetime.now(UTC) - entry.last_verified_at).total_seconds() / 86400,
        )
        recency_score = math.exp(-age_days / 45)
        return (
            url_score
            + path_score
            + fingerprint_score
            + entry.confidence * 3
            + outcome_score
            + recency_score
        )

    @staticmethod
    def _with_score(entry: MemoryEntry, score: float) -> MemoryEntry:
        return MemoryEntry(
            memory_id=entry.memory_id,
            scope=entry.scope,
            normalized_url=entry.normalized_url,
            path_template=entry.path_template,
            site_origin=entry.site_origin,
            kind=entry.kind,
            content=entry.content,
            page_fingerprint=entry.page_fingerprint,
            confidence=entry.confidence,
            evidence_id=entry.evidence_id,
            created_at=entry.created_at,
            last_verified_at=entry.last_verified_at,
            success_count=entry.success_count,
            failure_count=entry.failure_count,
            score=score,
        )

    @staticmethod
    def _program_from_row(row: sqlite3.Row) -> CollectionProgram:
        return CollectionProgram(
            program_id=str(row["id"]),
            scope=ExecutionScope(
                project_id=str(row["project_id"]),
                tenant_id=str(row["tenant_id"]),
                account_id=str(row["account_id"]),
            ),
            scenario_key=str(row["scenario_key"]),
            normalized_url=str(row["normalized_url"]),
            path_template=str(row["path_template"]),
            site_origin=str(row["site_origin"]),
            structure_fingerprint=str(row["structure_fingerprint"]),
            spec=json.loads(str(row["spec_json"])),
            summary=json.loads(str(row["summary_json"])),
            confidence=float(row["confidence"]),
            evidence_id=str(row["evidence_id"]),
            created_at=_parse_time(str(row["created_at"])),
            last_verified_at=_parse_time(str(row["last_verified_at"])),
            success_count=int(row["success_count"]),
            failure_count=int(row["failure_count"]),
            average_latency_ms=float(row["average_latency_ms"]),
            enabled=bool(row["enabled"]),
            metadata=json.loads(str(row["metadata_json"])),
        )

    @staticmethod
    def _plan_score(plan: VerifiedPlan) -> float:
        total = plan.success_count + plan.failure_count
        success_rate = plan.success_count / total if total else 0.5
        latency_bonus = 1 / (1 + plan.average_latency_ms / 1000) if plan.average_latency_ms else 0.5
        return plan.confidence * 3 + success_rate + latency_bonus

    @staticmethod
    def _program_score(program: CollectionProgram) -> float:
        total = program.success_count + program.failure_count
        success_rate = program.success_count / total if total else 0.5
        latency_bonus = (
            1 / (1 + program.average_latency_ms / 1000) if program.average_latency_ms else 0.5
        )
        return program.confidence * 3 + success_rate + latency_bonus

    @staticmethod
    def _looks_sensitive(value: str) -> bool:
        lowered = value.lower()
        return any(
            marker in lowered
            for marker in ("bearer ", "password=", "token=", "api_key=", "cookie=")
        )

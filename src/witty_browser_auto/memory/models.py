"""URL 记忆和快速计划的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from witty_browser_auto.domain.models import ActionKind, ExecutionScope


class MemoryKind(str, Enum):
    ATTENTION = "attention"
    LOAD_CONDITION = "load_condition"
    LOCATOR = "locator"
    RECOVERY = "recovery"
    NAVIGATION = "navigation"
    DATA_HINT = "data_hint"
    # 失败教训。与其他类型不同，它在任务失败或阻塞时也写回，用于避免重复犯同一个错。
    LESSON = "lesson"


# 站点级全局记忆使用保留作用域。验证码形态、列表结构、分页形态这类事实只取决于站点，
# 不取决于租户或账号，跨任务复用才能让智能体越用越快。
GLOBAL_SCOPE = ExecutionScope(
    project_id="__global__",
    tenant_id="__global__",
    account_id="__global__",
)


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    memory_id: str
    scope: ExecutionScope
    normalized_url: str
    path_template: str
    site_origin: str
    kind: MemoryKind
    content: dict[str, Any]
    page_fingerprint: str
    confidence: float
    evidence_id: str
    created_at: datetime
    last_verified_at: datetime
    success_count: int
    failure_count: int
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class PlanStep:
    action_kind: ActionKind
    target_role: str | None = None
    target_name: str | None = None
    input_key: str | None = None
    static_value: str | None = None
    expected_kind: str | None = None
    expected_value: str | None = None
    idempotent: bool = False

    def __post_init__(self) -> None:
        if self.action_kind is ActionKind.INPUT_TEXT and not self.input_key:
            raise ValueError("快速计划中的输入动作必须引用任务输入键")
        if self.input_key and self.static_value is not None:
            raise ValueError("快速计划不能同时保存任务输入键和静态值")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_kind": self.action_kind.value,
            "target_role": self.target_role,
            "target_name": self.target_name,
            "input_key": self.input_key,
            "static_value": self.static_value,
            "expected_kind": self.expected_kind,
            "expected_value": self.expected_value,
            "idempotent": self.idempotent,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PlanStep:
        return cls(
            action_kind=ActionKind(str(value["action_kind"])),
            target_role=value.get("target_role"),
            target_name=value.get("target_name"),
            input_key=value.get("input_key"),
            static_value=value.get("static_value"),
            expected_kind=value.get("expected_kind"),
            expected_value=value.get("expected_value"),
            idempotent=bool(value.get("idempotent", False)),
        )


@dataclass(frozen=True, slots=True)
class VerifiedPlan:
    plan_id: str
    scope: ExecutionScope
    scenario_key: str
    normalized_url: str
    path_template: str
    site_origin: str
    start_fingerprint: str
    steps: tuple[PlanStep, ...]
    confidence: float
    evidence_id: str
    created_at: datetime
    last_verified_at: datetime
    success_count: int = 0
    failure_count: int = 0
    average_latency_ms: float = 0.0
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CollectionProgram:
    """通过存储前验证门的采集程序：受控规格 + 结构指纹 + 复用统计。

    与 VerifiedPlan 的区别：计划固化的是逐步 UI 动作，程序固化的是一份可被
    固定代码整体重放的采集规格；重放前先做入口结构探针，失配即回退模型链路。
    """

    program_id: str
    scope: ExecutionScope
    scenario_key: str
    normalized_url: str
    path_template: str
    site_origin: str
    structure_fingerprint: str
    spec: dict[str, Any]
    summary: dict[str, Any]
    confidence: float
    evidence_id: str
    created_at: datetime
    last_verified_at: datetime
    success_count: int = 0
    failure_count: int = 0
    average_latency_ms: float = 0.0
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

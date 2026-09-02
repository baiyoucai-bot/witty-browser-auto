"""确定性失败分类契约；供工具执行层标注 `failure_kind`，不含模型调用或补丁执行。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class ToolFailureKind(str, Enum):
    """确定性区分页面失败、外部故障和本项目工具代码缺陷。"""

    REQUEST = "request"
    POLICY = "policy"
    ACTION = "action"
    VERIFICATION = "verification"
    INFRASTRUCTURE = "infrastructure"
    TOOL_DEFECT = "tool_defect"


class RepairStatus(str, Enum):
    DECLINED = "declined"
    FAILED = "failed"
    RESTART_REQUIRED = "restart_required"


@dataclass(frozen=True, slots=True)
class ToolFailureContext:
    task_id: str
    tool_name: str
    call_id: str
    current_url: str
    observation_fingerprint: str
    message: str
    exception_type: str
    frames: tuple[dict[str, Any], ...] = ()
    idempotent: bool = False
    outcome_known: bool | None = None
    code_version: str = ""


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    status: RepairStatus
    message: str
    repair_id: str = ""
    new_tool_version: str = ""
    rollback_version: str = ""
    changed_files: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "repair_id": self.repair_id,
            "new_tool_version": self.new_tool_version,
            "rollback_version": self.rollback_version,
            "changed_files": list(self.changed_files),
            "verification": list(self.verification),
            "metadata": self.metadata,
        }


class RepairCoordinator(Protocol):
    async def repair(self, context: ToolFailureContext) -> RepairOutcome: ...


class NoopRepairCoordinator:
    """未配置隔离补丁执行器时明确拒绝，不伪装成已完成修复。"""

    async def repair(self, context: ToolFailureContext) -> RepairOutcome:
        return RepairOutcome(
            status=RepairStatus.DECLINED,
            message=f"工具 {context.tool_name} 检测到内部缺陷，但工程修复执行器尚未启用",
        )

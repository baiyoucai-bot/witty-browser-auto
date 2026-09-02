"""确定性失败分类契约；不含模型调用或补丁执行。"""

from witty_browser_auto.runtime.repair import (
    NoopRepairCoordinator,
    RepairOutcome,
    RepairStatus,
    ToolFailureContext,
    ToolFailureKind,
)

__all__ = [
    "NoopRepairCoordinator",
    "RepairOutcome",
    "RepairStatus",
    "ToolFailureContext",
    "ToolFailureKind",
]

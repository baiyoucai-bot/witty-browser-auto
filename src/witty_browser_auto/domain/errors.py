"""项目统一错误类型。"""

from __future__ import annotations

from typing import Any


class RpaError(Exception):
    """带稳定错误码和上下文的基础异常。"""

    code = "RPA_ERROR"

    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.context = context or {}


class ConfigurationError(RpaError):
    code = "CONFIGURATION_ERROR"


class PolicyViolationError(RpaError):
    code = "POLICY_VIOLATION"


class BrowserLaunchError(RpaError):
    code = "BROWSER_LAUNCH_ERROR"


class CdpConnectionError(RpaError):
    code = "CDP_CONNECTION_ERROR"


class CdpCommandError(RpaError):
    code = "CDP_COMMAND_ERROR"

    def __init__(
        self,
        message: str,
        *,
        method: str,
        error_code: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        merged_context = {"method": method, "cdp_error_code": error_code, **(context or {})}
        super().__init__(message, context=merged_context)
        self.method = method
        self.error_code = error_code


class CdpDisconnectedError(CdpConnectionError):
    code = "CDP_DISCONNECTED"


class TargetNotFoundError(RpaError):
    code = "TARGET_NOT_FOUND"


class TargetAmbiguousError(RpaError):
    code = "TARGET_AMBIGUOUS"


class ActionVerificationError(RpaError):
    code = "ACTION_VERIFICATION_ERROR"


class ActionOutcomeUnknownError(RpaError):
    code = "ACTION_OUTCOME_UNKNOWN"


class TaskBlockedError(RpaError):
    code = "TASK_BLOCKED"


class CheckpointMismatchError(RpaError):
    code = "CHECKPOINT_MISMATCH"

"""与具体浏览器和模型供应商无关的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskState(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    OBSERVING = "observing"
    VERIFYING = "verifying"
    WAITING = "waiting"
    REPAIRING = "repairing"
    RESTARTING = "restarting"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class ActionKind(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    HOVER = "hover"
    VISUAL_CLICK = "visual_click"
    INSPECT_VISUAL_REGION = "inspect_visual_region"
    INPUT_TEXT = "input_text"
    SELECT = "select"
    DRAG = "drag"
    VISUAL_DRAG = "visual_drag"
    SCROLL = "scroll"
    WAIT = "wait"
    EVALUATE = "evaluate"
    SCREENSHOT = "screenshot"
    PRESS_KEY = "press_key"
    NAVIGATE_HISTORY = "navigate_history"
    UPLOAD_FILES = "upload_files"


class DragRiskClass(str, Enum):
    BUSINESS = "business"
    SECURITY = "security"
    UNKNOWN = "unknown"


class DecisionKind(str, Enum):
    TOOL_CALLS = "tool_calls"
    FINISH = "finish"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class ExecutionScope:
    project_id: str
    tenant_id: str = "default"
    account_id: str = "default"
    allowed_origins: tuple[str, ...] = ()

    @property
    def memory_key(self) -> str:
        return f"{self.project_id}:{self.tenant_id}:{self.account_id}"


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    goal: str
    start_url: str
    scope: ExecutionScope
    max_steps: int = 20
    timeout_seconds: float = 300.0
    allowed_actions: frozenset[ActionKind] = field(
        default_factory=lambda: frozenset(
            {
                ActionKind.NAVIGATE,
                ActionKind.CLICK,
                ActionKind.HOVER,
                ActionKind.VISUAL_CLICK,
                ActionKind.INSPECT_VISUAL_REGION,
                ActionKind.INPUT_TEXT,
                ActionKind.SELECT,
                ActionKind.DRAG,
                ActionKind.VISUAL_DRAG,
                ActionKind.SCROLL,
                ActionKind.WAIT,
                ActionKind.SCREENSHOT,
                ActionKind.PRESS_KEY,
                ActionKind.NAVIGATE_HISTORY,
                ActionKind.UPLOAD_FILES,
            }
        )
    )
    inputs: dict[str, Any] = field(default_factory=dict)
    # 运行时输入槽位参与检查点签名，具体值可由后续对话补充而不改变任务身份。
    input_slots: tuple[str, ...] = ()
    allow_security_challenge: bool = False
    trusted_challenge_origins: tuple[str, ...] = ()
    max_security_challenge_attempts: int = 1
    allow_visual_actions: bool = False
    allow_unknown_visual_drag: bool = False
    # 聊天工作台任务结束后保留可见浏览器，直到用户显式关闭或启动新任务替换它。
    keep_browser_open: bool = False
    workspace_root: str = ""
    output_directory: str = ""
    output_formats: tuple[str, ...] = ()
    # 生产只读环境的硬门控；开启后副作用工具在触碰浏览器前直接拒绝。
    read_only: bool = False

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("任务 ID 不能为空")
        if not self.goal.strip():
            raise ValueError("任务目标不能为空")
        if self.max_steps < 1:
            raise ValueError("最大步骤数必须大于零")
        if self.timeout_seconds <= 0:
            raise ValueError("任务超时时间必须大于零")
        if not isinstance(self.read_only, bool):
            raise ValueError("只读策略必须是布尔值")
        if self.max_security_challenge_attempts < 1:
            raise ValueError("安全挑战审计提示值必须大于零")
        if self.allow_unknown_visual_drag and not self.allow_visual_actions:
            raise ValueError("允许未知视觉拖拽前必须先授权视觉坐标动作")
        slots = self.input_slots or tuple(sorted(self.inputs))
        if len(set(slots)) != len(slots):
            raise ValueError("任务输入槽位不能重复")
        if any(
            not isinstance(slot, str)
            or not slot.strip()
            or len(slot) > 100
            or any(ord(character) < 32 for character in slot)
            for slot in slots
        ):
            raise ValueError("任务输入槽位格式无效")
        if missing_slots := set(self.inputs) - set(slots):
            raise ValueError(f"任务输入缺少已声明槽位：{', '.join(sorted(missing_slots))}")
        object.__setattr__(self, "input_slots", tuple(slots))
        if any(not origin.strip() for origin in self.trusted_challenge_origins):
            raise ValueError("企业受信挑战来源不能为空")
        if self.output_directory:
            output_directory = Path(self.output_directory).expanduser()
            if not output_directory.is_absolute():
                raise ValueError("输出目录必须是绝对路径")
            if "\x00" in self.output_directory or len(self.output_directory) > 1024:
                raise ValueError("输出目录格式无效")
        if self.workspace_root:
            workspace_root = Path(self.workspace_root).expanduser()
            if not workspace_root.is_absolute() or "\x00" in self.workspace_root:
                raise ValueError("项目工作区必须是有效的绝对路径")
        supported_formats = {"json", "csv", "xlsx"}
        if unsupported := set(self.output_formats) - supported_formats:
            raise ValueError(f"不支持的输出格式：{', '.join(sorted(unsupported))}")
        if len(set(self.output_formats)) != len(self.output_formats):
            raise ValueError("输出格式不能重复")

    def allows_security_challenge_at(self, origin: str) -> bool:
        return self.allow_security_challenge or origin in self.trusted_challenge_origins

    @property
    def allowed_drag_risks(self) -> frozenset[DragRiskClass]:
        risks = {DragRiskClass.BUSINESS}
        if self.allow_security_challenge:
            risks.add(DragRiskClass.SECURITY)
        return frozenset(risks)

    @property
    def allowed_visual_drag_risks(self) -> frozenset[DragRiskClass]:
        if not self.allow_visual_actions:
            return frozenset()
        risks = {DragRiskClass.BUSINESS}
        if self.allow_security_challenge:
            risks.add(DragRiskClass.SECURITY)
        if self.allow_unknown_visual_drag:
            risks.add(DragRiskClass.UNKNOWN)
        return frozenset(risks)


@dataclass(frozen=True, slots=True)
class DriverCapabilities:
    dom: bool = False
    accessibility: bool = False
    visual: bool = False
    network: bool = False
    files: bool = False
    storage: bool = False
    dialogs: bool = False
    emulation: bool = False
    forms: bool = False
    storage_state: bool = False
    element_drag: bool = False
    pdf_export: bool = False
    performance: bool = False
    windows: bool = False
    javascript: bool = False


@dataclass(frozen=True, slots=True)
class BoundingBox:
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)


@dataclass(frozen=True, slots=True)
class LocatorRecipe:
    strategy: str
    value: str | None = None
    role: str | None = None
    name: str | None = None
    backend_node_id: int | None = None
    frame_id: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateTarget:
    target_id: str
    role: str
    name: str
    text: str
    confidence: float
    reasons: tuple[str, ...]
    recipe: LocatorRecipe
    box: BoundingBox | None = None
    disabled: bool = False
    drag_risk: DragRiskClass = DragRiskClass.UNKNOWN
    drag_risk_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Observation:
    surface_id: str
    url: str
    title: str
    version: int
    fingerprint: str
    summary: str
    candidates: tuple[CandidateTarget, ...]
    visual_drag_risk: DragRiskClass = DragRiskClass.UNKNOWN
    visual_drag_risk_reasons: tuple[str, ...] = ()
    captured_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExpectedCondition:
    kind: str
    value: str
    timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class DragPoint:
    """相对目标中心的单个拖拽轨迹点。"""

    dx: float
    dy: float
    delay_ms: int = 16

    def __post_init__(self) -> None:
        if not isfinite(self.dx) or not isfinite(self.dy):
            raise ValueError("拖拽轨迹坐标必须是有限数值")
        if abs(self.dx) > 3000 or abs(self.dy) > 3000:
            raise ValueError("单个拖拽轨迹点不能超出目标中心 3000 像素")
        if not 0 <= self.delay_ms <= 200:
            raise ValueError("拖拽轨迹点延时必须在 0 到 200 毫秒之间")


@dataclass(frozen=True, slots=True)
class VisualDragPoint:
    """绑定当前截图的视口比例拖拽点。"""

    x_ratio: float
    y_ratio: float
    delay_ms: int = 16

    def __post_init__(self) -> None:
        if not isfinite(self.x_ratio) or not isfinite(self.y_ratio):
            raise ValueError("视觉拖拽坐标必须是有限数值")
        if not 0 <= self.x_ratio <= 1 or not 0 <= self.y_ratio <= 1:
            raise ValueError("视觉拖拽坐标比例必须在 0 到 1 之间")
        if not 0 <= self.delay_ms <= 200:
            raise ValueError("视觉拖拽轨迹点延时必须在 0 到 200 毫秒之间")


@dataclass(frozen=True, slots=True)
class ActionCommand:
    action_id: str
    kind: ActionKind
    target_id: str | None = None
    locator: LocatorRecipe | None = None
    value: str | None = None
    url: str | None = None
    script: str | None = None
    trajectory: tuple[DragPoint, ...] = ()
    visual_trajectory: tuple[VisualDragPoint, ...] = ()
    visual_x_ratio: float | None = None
    visual_y_ratio: float | None = None
    visual_clip: tuple[float, float, float, float] | None = None
    observation_fingerprint: str | None = None
    screenshot_fingerprint: str | None = None
    visual_confidence: float | None = None
    visual_drag_strategy: str | None = None
    visual_drag_signature: str | None = None
    allow_dynamic_visual_frame: bool = False
    security_challenge: bool = False
    drag_risk: DragRiskClass | None = None
    drag_risk_reasons: tuple[str, ...] = ()
    expected: ExpectedCondition | None = None
    timeout_seconds: float = 15.0
    idempotent: bool = False
    pointer_button: str = "left"
    click_count: int = 1
    file_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("动作超时时间必须大于零")
        if self.kind is ActionKind.NAVIGATE and not self.url:
            raise ValueError("导航动作必须提供 URL")
        if self.pointer_button not in {"left", "right", "middle"}:
            raise ValueError("鼠标按键只能是 left、right 或 middle")
        if not 1 <= self.click_count <= 3:
            raise ValueError("点击次数必须在 1 到 3 之间")
        if self.kind is not ActionKind.CLICK and (
            self.pointer_button != "left" or self.click_count != 1
        ):
            raise ValueError("只有点击动作可以指定鼠标按键与点击次数")
        if self.kind in {
            ActionKind.CLICK,
            ActionKind.HOVER,
            ActionKind.INPUT_TEXT,
            ActionKind.SELECT,
            ActionKind.UPLOAD_FILES,
        }:
            if not self.target_id and self.locator is None:
                raise ValueError(f"{self.kind.value} 动作必须提供目标区域")
            if self.target_id and self.locator is not None:
                raise ValueError(f"{self.kind.value} 动作必须且只能提供 target_id 或显式定位器")
        if self.kind is ActionKind.DRAG:
            if not self.target_id or self.locator is not None:
                raise ValueError(f"{self.kind.value} 动作必须提供目标区域")
        if self.kind is ActionKind.INPUT_TEXT and self.value is None:
            raise ValueError("输入动作必须提供文本")
        if self.kind is ActionKind.UPLOAD_FILES:
            if not self.file_paths:
                raise ValueError("上传动作必须提供至少一个本地文件路径")
            if self.idempotent:
                raise ValueError("上传动作必须按非幂等动作执行")
        elif self.file_paths:
            raise ValueError("只有上传动作可以携带本地文件路径")
        if self.kind is ActionKind.EVALUATE and not self.script:
            raise ValueError("JavaScript 动作必须提供脚本")
        if self.kind is ActionKind.PRESS_KEY and not self.value:
            raise ValueError("按键动作必须提供已编译的按键规格")
        if self.kind is ActionKind.NAVIGATE_HISTORY and not self.value:
            raise ValueError("页面历史动作必须提供 back、forward 或 reload")
        if self.kind is ActionKind.DRAG:
            if not 2 <= len(self.trajectory) <= 120:
                raise ValueError("拖拽轨迹必须包含 2 到 120 个点")
            first = self.trajectory[0]
            if first.dx != 0 or first.dy != 0:
                raise ValueError("拖拽轨迹必须从目标区域中心开始")
            if sum(point.delay_ms for point in self.trajectory) > 5000:
                raise ValueError("拖拽轨迹总时长不能超过 5 秒")
            if self.idempotent:
                raise ValueError("拖拽动作必须按非幂等动作执行")
            if self.drag_risk is None:
                raise ValueError("拖拽动作必须携带执行层风险分类")
        elif self.trajectory:
            raise ValueError("只有拖拽动作可以携带轨迹")
        visual_action = self.kind in {
            ActionKind.VISUAL_CLICK,
            ActionKind.INSPECT_VISUAL_REGION,
            ActionKind.VISUAL_DRAG,
        }
        if visual_action:
            if not self.observation_fingerprint:
                raise ValueError("视觉动作必须绑定页面观察指纹")
            if not self.screenshot_fingerprint:
                raise ValueError("视觉动作必须绑定当前截图指纹")
            if self.visual_confidence is None or not 0.8 <= self.visual_confidence <= 1:
                raise ValueError("视觉动作置信度必须在 0.8 到 1 之间")
        elif (
            self.observation_fingerprint is not None
            or self.screenshot_fingerprint is not None
            or self.visual_confidence is not None
        ):
            raise ValueError("只有视觉动作可以携带视觉绑定信息")
        if self.kind is ActionKind.VISUAL_CLICK:
            if self.target_id:
                raise ValueError("视觉点击不能携带语义目标 ID")
            if self.visual_x_ratio is None or self.visual_y_ratio is None:
                raise ValueError("视觉点击必须提供视口比例坐标")
            if not isfinite(self.visual_x_ratio) or not isfinite(self.visual_y_ratio):
                raise ValueError("视觉点击坐标必须是有限数值")
            if not 0 <= self.visual_x_ratio <= 1 or not 0 <= self.visual_y_ratio <= 1:
                raise ValueError("视觉点击坐标比例必须在 0 到 1 之间")
            if self.idempotent:
                raise ValueError("视觉点击必须按非幂等动作执行")
        elif self.visual_x_ratio is not None or self.visual_y_ratio is not None:
            raise ValueError("只有视觉点击可以携带单点视口坐标")
        if self.kind is ActionKind.INSPECT_VISUAL_REGION:
            if self.visual_clip is None:
                raise ValueError("视觉区域观察必须提供视口裁剪范围")
            x_ratio, y_ratio, width_ratio, height_ratio = self.visual_clip
            if not all(isfinite(value) for value in (x_ratio, y_ratio, width_ratio, height_ratio)):
                raise ValueError("视觉区域裁剪比例必须是有限数值")
            if not 0 <= x_ratio <= 1 or not 0 <= y_ratio <= 1:
                raise ValueError("视觉区域裁剪起点比例必须在 0 到 1 之间")
            if not 0.05 <= width_ratio <= 1 or not 0.05 <= height_ratio <= 1:
                raise ValueError("视觉区域裁剪宽高比例必须在 0.05 到 1 之间")
            if x_ratio + width_ratio > 1 or y_ratio + height_ratio > 1:
                raise ValueError("视觉区域裁剪范围不能超出当前视口")
            if not self.idempotent:
                raise ValueError("视觉区域观察必须按只读幂等动作执行")
        elif self.visual_clip is not None:
            raise ValueError("只有视觉区域观察可以携带视口裁剪范围")
        if self.kind is ActionKind.VISUAL_DRAG:
            if self.target_id:
                raise ValueError("视觉拖拽不能携带语义目标 ID")
            if not 2 <= len(self.visual_trajectory) <= 120:
                raise ValueError("视觉拖拽轨迹必须包含 2 到 120 个点")
            if sum(point.delay_ms for point in self.visual_trajectory) > 5000:
                raise ValueError("视觉拖拽轨迹总时长不能超过 5 秒")
            if self.idempotent:
                raise ValueError("视觉拖拽动作必须按非幂等动作执行")
            if self.drag_risk is None:
                raise ValueError("视觉拖拽动作必须携带执行层风险分类")
        elif self.visual_trajectory:
            raise ValueError("只有视觉拖拽动作可以携带视口轨迹")
        if self.allow_dynamic_visual_frame and (
            self.kind is not ActionKind.VISUAL_DRAG
            or not self.security_challenge
            or self.drag_risk is not DragRiskClass.SECURITY
        ):
            raise ValueError("动态视觉帧只允许用于已授权且明确分类的安全挑战")
        if self.security_challenge and self.kind not in {
            ActionKind.DRAG,
            ActionKind.VISUAL_DRAG,
        }:
            raise ValueError("安全挑战标记只允许用于拖拽动作")
        if self.kind not in {ActionKind.DRAG, ActionKind.VISUAL_DRAG} and (
            self.drag_risk is not None or self.drag_risk_reasons
        ):
            raise ValueError("只有拖拽动作可以携带风险分类")


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    action_id: str
    success: bool
    outcome_known: bool
    message: str
    duration_ms: float
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    success: bool
    reason: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    kind: str
    path: str | None = None
    summary: str = ""


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    tool_calls: tuple[ModelToolCall, ...]
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str = ""


@dataclass(frozen=True, slots=True)
class ModelStreamEvent:
    kind: str
    text: str = ""
    response: ModelResponse | None = None


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    state: TaskState
    message: str
    steps: int
    started_at: datetime
    finished_at: datetime
    evidence: tuple[EvidenceRef, ...] = ()
    output: dict[str, Any] = field(default_factory=dict)

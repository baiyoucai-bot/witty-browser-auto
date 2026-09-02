"""大模型可见工具定义与确定性执行策略。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import traceback
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from witty_browser_auto.agent import (
    capability_tools,
    dialog_tools,
    element_tools,
    emulation_tools,
    file_tools,
    form_tools,
    frame_tools,
    locator_tools,
    network_tools,
    page_tools,
    script_tools,
    storage_tools,
    tab_tools,
    traffic_tools,
)
from witty_browser_auto.agent import page_diagnostics as diagnostics
from witty_browser_auto.agent.collection_program import (
    probe_entry_until_ready,
    scenario_key,
    verify_and_promote_collection_program,
)
from witty_browser_auto.agent.crawl_tools import (
    CRAWL_TOOL_NAMES,
    DEFAULT_CRAWL_AGENT,
    CrawlPolicyStore,
    execute_check_crawl_policy,
    origin_of,
)
from witty_browser_auto.agent.drag_trajectory import (
    VISUAL_DRAG_MOTION_PROFILES,
    build_drag_trajectory,
    build_human_visual_drag_trajectory,
)
from witty_browser_auto.agent.navigation_policy import (
    CLICK_VERIFICATION_TIMEOUT_SECONDS,
    assert_navigation_allowed,
    condition_visible_before_action,
    is_read_only_click,
    read_only_click_fallback_condition,
)
from witty_browser_auto.agent.tool_schemas import (
    CURRENT_TARGET_REFERENCE,
    SUPPORTED_EXPECTED_KINDS,
)
from witty_browser_auto.agent.tool_schemas import (
    TOOL_SCHEMAS as TOOL_SCHEMAS,
)
from witty_browser_auto.agent.visual_geometry import (
    challenge_refresh_condition,
    drag_challenge_condition,
    security_drag_geometry_error,
    security_drag_geometry_ratios,
)
from witty_browser_auto.browser.annotation import (
    DEFAULT_MAX_LABELS,
    build_annotation_labels,
)
from witty_browser_auto.browser.mouse import (
    VISUAL_DRAG_APPROACH_DURATION_MS,
    VISUAL_DRAG_APPROACH_POINTS,
    resolve_pointer,
)
from witty_browser_auto.browser.pacing import HostPacer
from witty_browser_auto.browser.privacy import capture_masked_evidence, mask_task_inputs
from witty_browser_auto.domain.errors import PolicyViolationError, RpaError, TargetNotFoundError
from witty_browser_auto.domain.extraction import (
    CollectionExtractionResult,
    CollectionExtractionSpec,
    collection_spec_from_inspection,
    requires_record_details,
    sanitize_collection_inspection,
)
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionKind,
    ActionReceipt,
    CandidateTarget,
    DragRiskClass,
    EvidenceRef,
    ExpectedCondition,
    ModelToolCall,
    Observation,
    TaskSpec,
    VerificationResult,
)
from witty_browser_auto.domain.protocols import (
    AutomationDriver,
    NetworkDataExtractor,
    StructuredDataExtractor,
)
from witty_browser_auto.extensions.runtime import AgentExtensionRuntime
from witty_browser_auto.memory.background import BackgroundMemoryRuntime
from witty_browser_auto.memory.models import PlanStep
from witty_browser_auto.memory.url import normalize_url
from witty_browser_auto.network.inspection import NetworkTrafficInspector
from witty_browser_auto.runtime.repair import ToolFailureKind
from witty_browser_auto.security.redaction import redact_task_inputs, sanitize_url_for_storage
from witty_browser_auto.toolkit.catalog import BROWSER_TOOLS
from witty_browser_auto.toolkit.script_export import ActionScriptLog

logger = logging.getLogger(__name__)

_VISUAL_DRAG_GEOMETRY_MODES = ("track", "model")


class RepeatedChallengeStrategyError(PolicyViolationError):
    """同一挑战回合重复了已经执行过的识别或动作策略。"""


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    call_id: str
    name: str
    success: bool
    message: str
    failure_kind: ToolFailureKind | None = None
    idempotent: bool = False
    receipt: ActionReceipt | None = None
    verification: VerificationResult | None = None
    plan_step: PlanStep | None = None
    evidence: EvidenceRef | None = None
    data: dict[str, Any] = field(default_factory=dict)
    counts_as_action: bool = True
    model_data: dict[str, Any] | None = None
    # 页面动作之后的新观察。由工具库门面在动作收口后附上，让调用方在同一次返回里
    # 既拿到动作结论也拿到新页面的候选，省掉紧随其后的一次 observe 往返。
    observation: Observation | None = None

    def model_payload(self) -> str:
        """模型只看到 `model_data`；未单独提供时才回退到调用方视图。"""

        return json.dumps(
            {
                "success": self.success,
                "message": self.message,
                "data": self.data if self.model_data is None else self.model_data,
                "verified": self.verification.success if self.verification else None,
            },
            ensure_ascii=False,
        )


class ToolExecutor:
    def __init__(
        self,
        driver: AutomationDriver,
        task: TaskSpec,
        *,
        visual_context_available: bool = False,
        structured_extractor: StructuredDataExtractor | None = None,
        network_data_extractor: NetworkDataExtractor | None = None,
        network_traffic_inspector: NetworkTrafficInspector | None = None,
        extension_runtime: AgentExtensionRuntime | None = None,
        memory_runtime: BackgroundMemoryRuntime | None = None,
        respect_robots: bool = False,
        min_request_interval_ms: float = 0.0,
        crawl_agent: str = DEFAULT_CRAWL_AGENT,
    ) -> None:
        self.driver = driver
        self.task = task
        self.respect_robots = respect_robots
        # 只要设了默认间隔或打开了 robots 遵守，就需要一把节奏阀门。
        self.crawl_pacer = (
            HostPacer(min_request_interval_ms)
            if min_request_interval_ms > 0 or respect_robots
            else None
        )
        self.crawl_policies = CrawlPolicyStore(agent=crawl_agent, pacer=self.crawl_pacer)
        self.visual_context_available = visual_context_available
        self.structured_extractor = structured_extractor
        self.network_data_extractor = network_data_extractor
        self.network_traffic_inspector = network_traffic_inspector
        self.extension_runtime = extension_runtime
        self.memory_runtime = memory_runtime
        self.network_data_inspected = False
        self.network_data_exhausted = False
        self.network_inspection: dict[str, Any] | None = None
        self._network_candidates_before_page_change: set[str] = set()
        self.exported_network_candidate_ids: set[str] = set()
        self.latest_network_extraction_result: network_tools.NetworkDataExportResult | None = None
        self.latest_extraction_result: CollectionExtractionResult | None = None
        # 最近一次成功闭合采集的规格与入口 URL，供采集程序验证门使用。
        self.latest_extraction_spec: CollectionExtractionSpec | None = None
        self.latest_extraction_entry_url: str | None = None
        self.collection_inspected = False
        self.collection_inspection: dict[str, Any] | None = None
        self.capability_gap_reported = False
        self.page_diagnostics_available = diagnostics.diagnostics_available(driver)
        self.tab_management_available = tab_tools.tabs_available(driver)
        self.element_inspection_available = element_tools.element_inspection_available(driver)
        self.element_screenshot_available = element_tools.element_screenshot_available(driver)
        self.frame_inspection_available = frame_tools.frame_inspection_available(driver)
        self.downloads_available = file_tools.downloads_available(driver)
        self.storage_available = storage_tools.storage_available(driver)
        self.dialogs_available = dialog_tools.dialogs_available(driver)
        self.emulation_available = emulation_tools.emulation_available(driver)
        self.forms_available = form_tools.forms_available(driver)
        self.storage_state_available = form_tools.storage_state_available(driver)
        self.element_drag_available = page_tools.element_drag_available(driver)
        self.pdf_export_available = page_tools.pdf_export_available(driver)
        self.performance_available = page_tools.performance_available(driver)
        self.action_log = ActionScriptLog()
        self.high_risk_drag_attempts = 0
        self.security_challenge_active = False
        self.security_challenge_phase = "idle"
        self.security_challenge_drag_strategies: list[str] = []
        self.security_challenge_text_signatures: list[str] = []

    @property
    def security_challenge_text_entered(self) -> bool:
        return self.security_challenge_phase == "text_entered"

    @property
    def security_challenge_awaiting_result(self) -> bool:
        return self.security_challenge_phase == "awaiting_result"

    @property
    def collection_candidate_ids(self) -> tuple[str, ...]:
        candidates = (
            self.collection_inspection.get("candidates", []) if self.collection_inspection else []
        )
        return tuple(
            str(candidate["candidate_id"])
            for candidate in candidates
            if isinstance(candidate, Mapping)
            and isinstance(candidate.get("candidate_id"), str)
            and isinstance(candidate.get("child_hints"), list)
            and bool(candidate["child_hints"])
        )

    @property
    def network_candidate_ids(self) -> tuple[str, ...]:
        candidates = (
            self.network_inspection.get("candidates", []) if self.network_inspection else []
        )
        return tuple(
            str(candidate["candidate_id"])
            for candidate in candidates
            if isinstance(candidate, Mapping)
            and isinstance(candidate.get("candidate_id"), str)
            and candidate["candidate_id"] not in self.exported_network_candidate_ids
        )

    def mark_security_challenge_failed(self) -> None:
        if self.security_challenge_active:
            self.security_challenge_phase = "ready"

    def resume_security_challenge(self) -> None:
        """恢复当前挑战，不重置审计计数或已失败策略历史。"""

        self.security_challenge_active = True
        self.security_challenge_phase = "ready"

    def restore_security_challenge(
        self,
        *,
        attempts: int,
        phase: str,
        drag_strategies: tuple[str, ...] = (),
        text_signatures: tuple[str, ...] = (),
    ) -> None:
        if attempts < 0:
            raise ValueError("恢复的验证码尝试次数不能为负数")
        if phase not in {"idle", "ready", "text_entered", "awaiting_result", "waiting_user"}:
            raise ValueError("恢复的验证码阶段无效")
        if phase == "idle":
            # 兼容旧检查点中“挑战已清除但尝试次数未归零”的不一致状态。
            self.clear_security_challenge()
            return
        self.high_risk_drag_attempts = attempts
        self.security_challenge_phase = phase
        self.security_challenge_active = phase != "idle"
        self.security_challenge_drag_strategies = list(dict.fromkeys(drag_strategies))[-12:]
        self.security_challenge_text_signatures = list(dict.fromkeys(text_signatures))[-12:]

    def clear_security_challenge(self) -> None:
        """结束已确认消失的挑战回合；后续新挑战使用独立预算与策略历史。"""

        self.high_risk_drag_attempts = 0
        self.security_challenge_active = False
        self.security_challenge_phase = "idle"
        self.security_challenge_drag_strategies.clear()
        self.security_challenge_text_signatures.clear()

    async def execute(
        self,
        call: ModelToolCall,
        observation: Observation,
    ) -> ToolExecutionResult:
        """执行一次工具调用，并把已验证的页面动作登记进可导出脚本。

        登记放在这层包装而不是主流程里，是因为主流程有十几条提前返回的分支，
        逐条补记录必然漏掉其中几条。
        """

        if self.task.read_only and self._requires_write_permission(call):
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                f"当前任务处于只读环境，工具 {call.name} 被安全策略拒绝",
                failure_kind=ToolFailureKind.POLICY,
                idempotent=True,
                counts_as_action=False,
                data={"read_only": True, "tool": call.name},
                model_data={"read_only": True, "tool": call.name},
            )

        if call.name == "export_action_script":
            result = await self._export_action_script(call)
        elif call.name in dialog_tools.DIALOG_TOOL_NAMES:
            result = self._handle_dialog(call)
        elif call.name in emulation_tools.EMULATION_TOOL_NAMES:
            result = await self._emulate_environment(call)
        elif call.name in form_tools.FORM_TOOL_NAMES:
            result = await self._run_form_tool(call)
        elif call.name in page_tools.PAGE_TOOL_NAMES:
            result = await self._run_page_tool(call, observation)
        else:
            result = await self._execute(call, observation)
        self.action_log.record(
            tool=call.name,
            arguments=call.arguments,
            observation=observation,
            success=result.success,
            counts_as_action=result.counts_as_action,
        )
        return result

    @staticmethod
    def _requires_write_permission(call: ModelToolCall) -> bool:
        """只读任务下允许对话框 inspect，但禁止所有会改变业务或会话状态的调用。"""

        # MCP/Skill 扩展的工具没有本地 ToolDefinition，生产只读模式下默认按有副作用处理。
        if call.name not in BROWSER_TOOLS:
            return True
        definition = BROWSER_TOOLS.get(call.name)
        if not definition.requires_write_permission:
            return False
        if call.name == "handle_dialog" and call.arguments.get("action") == "inspect":
            return False
        if call.name == "manage_storage_state" and call.arguments.get("operation") == "export":
            return False
        return True

    async def _run_page_tool(
        self,
        call: ModelToolCall,
        observation: Observation,
    ) -> ToolExecutionResult:
        try:
            if call.name == "drag_to_element":
                outcome = await page_tools.execute_drag_to_element(
                    call.arguments, driver=self.driver, observation=observation
                )
            elif call.name == "save_pdf":
                outcome = await page_tools.execute_save_pdf(call.arguments, driver=self.driver)
            elif call.name == "read_page_markdown":
                outcome = await page_tools.execute_read_page_markdown(
                    call.arguments, driver=self.driver
                )
            elif call.name == "list_page_links":
                outcome = await page_tools.execute_list_page_links(
                    call.arguments, driver=self.driver
                )
            else:
                outcome = await page_tools.execute_measure_performance(
                    call.arguments, driver=self.driver
                )
        except ValueError as exc:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                str(exc),
                failure_kind=ToolFailureKind.REQUEST,
                idempotent=True,
                counts_as_action=False,
            )
        return ToolExecutionResult(
            call.call_id,
            call.name,
            outcome.success,
            outcome.message,
            failure_kind=None if outcome.success else ToolFailureKind.VERIFICATION,
            # 拖放改变页面状态且不可安全重放；导出与采集都是只读的。
            idempotent=call.name != "drag_to_element",
            counts_as_action=call.name == "drag_to_element",
            data=outcome.data,
            model_data=outcome.model_data,
        )

    async def _run_form_tool(self, call: ModelToolCall) -> ToolExecutionResult:
        try:
            if call.name == "fill_form":
                outcome = await form_tools.execute_fill_form(
                    call.arguments, driver=self.driver, task=self.task
                )
            elif call.name == "wait_for_condition":
                outcome = await form_tools.execute_wait_for_condition(
                    call.arguments, driver=self.driver
                )
            else:
                artifact_root = getattr(self.driver, "artifact_root", None)
                if artifact_root is None:
                    raise ValueError("当前驱动没有可写的产物目录，无法保存会话态")
                outcome = await form_tools.execute_storage_state(
                    call.arguments,
                    driver=self.driver,
                    task=self.task,
                    artifact_root=artifact_root,
                )
        except ValueError as exc:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                str(exc),
                failure_kind=ToolFailureKind.REQUEST,
                idempotent=True,
                counts_as_action=False,
            )
        return ToolExecutionResult(
            call.call_id,
            call.name,
            outcome.success,
            outcome.message,
            failure_kind=None if outcome.success else ToolFailureKind.VERIFICATION,
            idempotent=True,
            # 只有填写改变了页面状态，等待与会话态存取都不算动作步数。
            counts_as_action=call.name == "fill_form",
            data=outcome.data,
            model_data=outcome.model_data,
        )

    async def _emulate_environment(self, call: ModelToolCall) -> ToolExecutionResult:
        try:
            outcome = await emulation_tools.execute_emulation_tool(
                call.arguments, driver=self.driver
            )
        except ValueError as exc:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                str(exc),
                failure_kind=ToolFailureKind.REQUEST,
                idempotent=True,
                counts_as_action=False,
            )
        return ToolExecutionResult(
            call.call_id,
            call.name,
            True,
            outcome.message,
            idempotent=True,
            counts_as_action=False,
            data=outcome.data,
        )

    def _handle_dialog(self, call: ModelToolCall) -> ToolExecutionResult:
        try:
            outcome = dialog_tools.execute_dialog_tool(
                call.arguments,
                driver=self.driver,
                task=self.task,
            )
        except ValueError as exc:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                str(exc),
                failure_kind=ToolFailureKind.REQUEST,
                idempotent=True,
                counts_as_action=False,
            )
        return ToolExecutionResult(
            call.call_id,
            call.name,
            True,
            outcome.message,
            idempotent=True,
            counts_as_action=False,
            data=outcome.data,
            model_data=outcome.model_data,
        )

    async def _export_action_script(self, call: ModelToolCall) -> ToolExecutionResult:
        try:
            outcome = script_tools.execute_script_tool(
                call.arguments,
                log=self.action_log,
                task=self.task,
            )
        except ValueError as exc:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                str(exc),
                failure_kind=ToolFailureKind.REQUEST,
                idempotent=True,
                counts_as_action=False,
            )
        return ToolExecutionResult(
            call.call_id,
            call.name,
            True,
            outcome.message,
            idempotent=True,
            counts_as_action=False,
            data=outcome.data,
            model_data=outcome.model_data,
        )

    async def _execute(
        self,
        call: ModelToolCall,
        observation: Observation,
    ) -> ToolExecutionResult:
        evidence: EvidenceRef | None = None
        command: ActionCommand | None = None
        receipt: ActionReceipt | None = None
        verification: VerificationResult | None = None
        generated_security_challenge = False
        generated_security_signature: str | None = None
        try:
            if self.extension_runtime is not None and self.extension_runtime.handles(call.name):
                try:
                    outcome = await self.extension_runtime.execute(call.name, call.arguments)
                except ValueError as exc:
                    return ToolExecutionResult(
                        call.call_id,
                        call.name,
                        False,
                        str(exc),
                        failure_kind=ToolFailureKind.REQUEST,
                        idempotent=True,
                        counts_as_action=False,
                    )
                except Exception as exc:
                    logger.warning(
                        "扩展工具执行失败",
                        extra={"tool": call.name, "exception_type": type(exc).__name__},
                    )
                    return ToolExecutionResult(
                        call.call_id,
                        call.name,
                        False,
                        f"扩展工具执行失败：{exc}",
                        failure_kind=ToolFailureKind.INFRASTRUCTURE,
                        idempotent=True,
                        counts_as_action=False,
                    )
                return ToolExecutionResult(
                    call.call_id,
                    call.name,
                    outcome.success,
                    outcome.message,
                    failure_kind=(None if outcome.success else ToolFailureKind.INFRASTRUCTURE),
                    idempotent=outcome.idempotent,
                    data=outcome.data,
                    counts_as_action=outcome.counts_as_action,
                )
            if call.name == diagnostics.PAGE_DIAGNOSTIC_TOOL_NAME:
                outcome = await diagnostics.execute_page_diagnostic_tool(
                    call, self.driver, task_inputs=self.task.inputs
                )
                self.page_diagnostics_available = False
                return ToolExecutionResult(
                    call.call_id,
                    call.name,
                    outcome.success,
                    outcome.message,
                    idempotent=True,
                    data=outcome.data,
                )
            if call.name in element_tools.ELEMENT_READ_TOOL_NAMES:
                read_outcome = await element_tools.execute_element_read_tool(
                    call, self.driver, task_inputs=self.task.inputs
                )
                return ToolExecutionResult(
                    call.call_id,
                    call.name,
                    read_outcome.success,
                    read_outcome.message,
                    idempotent=True,
                    data=read_outcome.data,
                    counts_as_action=False,
                )
            if call.name in frame_tools.FRAME_TOOL_NAMES:
                frame_outcome = await frame_tools.execute_frame_tool(call, self.driver)
                return ToolExecutionResult(
                    call.call_id,
                    call.name,
                    frame_outcome.success,
                    frame_outcome.message,
                    idempotent=True,
                    data=frame_outcome.data,
                    counts_as_action=False,
                )
            if call.name in tab_tools.TAB_TOOL_NAMES:
                if call.name == "open_tab":
                    # 新标签页同样是一次新地址访问，与 navigate 走同一道抓取闸门。
                    refusal = await self._apply_crawl_policy(str(call.arguments.get("url") or ""))
                    if refusal is not None:
                        return self._crawl_refusal(call, refusal)
                tab_outcome = await tab_tools.execute_tab_tool(
                    call, self.driver, task=self.task, task_inputs=self.task.inputs
                )
                if tab_outcome.page_changed:
                    # 切换或关闭当前页后，旧页面的 DOM 与网络观察全部作废。
                    self._invalidate_page_bound_inspections()
                return ToolExecutionResult(
                    call.call_id,
                    call.name,
                    tab_outcome.success,
                    tab_outcome.message,
                    idempotent=tab_outcome.idempotent,
                    data=tab_outcome.data,
                    counts_as_action=tab_outcome.counts_as_action,
                )
            if call.name in file_tools.DOWNLOAD_TOOL_NAMES:
                download_outcome = await file_tools.execute_download_tool(
                    call, self.driver, task_inputs=self.task.inputs
                )
                return ToolExecutionResult(
                    call.call_id,
                    call.name,
                    download_outcome.success,
                    download_outcome.message,
                    idempotent=True,
                    data=download_outcome.data,
                    counts_as_action=False,
                )
            if call.name in storage_tools.STORAGE_TOOL_NAMES:
                storage_outcome = await storage_tools.execute_storage_tool(
                    call,
                    self.driver,
                    task=self.task,
                    task_inputs=self.task.inputs,
                )
                return ToolExecutionResult(
                    call.call_id,
                    call.name,
                    storage_outcome.success,
                    storage_outcome.message,
                    idempotent=storage_outcome.idempotent,
                    data=storage_outcome.data,
                    model_data=storage_outcome.model_data,
                    counts_as_action=storage_outcome.counts_as_action,
                )
            if call.name in traffic_tools.TRAFFIC_TOOL_NAMES:
                traffic_outcome = await traffic_tools.execute_traffic_tool(
                    call.name,
                    call.arguments,
                    self.network_traffic_inspector,
                    task_inputs=self.task.inputs,
                )
                return ToolExecutionResult(
                    call.call_id,
                    call.name,
                    traffic_outcome.success,
                    traffic_outcome.message,
                    idempotent=traffic_outcome.idempotent,
                    data=traffic_outcome.data,
                    model_data=traffic_outcome.model_data,
                    evidence=traffic_outcome.evidence,
                    counts_as_action=traffic_outcome.counts_as_action,
                )
            if call.name in network_tools.NETWORK_TOOL_NAMES:
                if call.name == "export_network_response":
                    selection_error = self._network_batch_selection_error(call.arguments)
                    if selection_error:
                        self.network_data_inspected = False
                        self.network_data_exhausted = True
                        await self._cache_collection_inspection()
                        fallback = self._dom_fallback_data()
                        return ToolExecutionResult(
                            call.call_id,
                            call.name,
                            False,
                            selection_error,
                            failure_kind=ToolFailureKind.REQUEST,
                            idempotent=True,
                            data=fallback,
                            counts_as_action=False,
                        )
                try:
                    outcome = await network_tools.execute_network_tool(
                        call.name,
                        call.arguments,
                        self.network_data_extractor,
                        task_inputs=self.task.inputs,
                    )
                except Exception:
                    if call.name == "export_network_response":
                        # Captured response IDs are ephemeral. A failed export must reopen
                        # inspection instead of trapping the next round in export/block.
                        self.network_data_inspected = False
                    raise
                if outcome.success and call.name == "inspect_network_data":
                    candidates = outcome.data.get("candidates")
                    if isinstance(candidates, list):
                        candidates[:] = [
                            candidate
                            for candidate in candidates
                            if isinstance(candidate, Mapping)
                            and candidate.get("candidate_id")
                            not in self.exported_network_candidate_ids
                        ]
                        self._filter_network_candidates_after_page_change(candidates)
                    self.network_inspection = outcome.data
                    self.network_data_inspected = bool(self.network_candidate_ids)
                    self.network_data_exhausted = not self.network_data_inspected
                if outcome.export_result is not None:
                    self.latest_network_extraction_result = outcome.export_result
                    self.exported_network_candidate_ids.add(outcome.export_result.candidate_id)
                    if not outcome.export_result.has_strong_completion_evidence:
                        self.network_data_inspected = False
                        self.network_data_exhausted = True
                        await self._cache_collection_inspection()
                if outcome.success and call.name == "manage_network_route":
                    operation = call.arguments.get("operation")
                    if operation in {"add", "remove"}:
                        self.network_inspection = {}
                        self.network_data_inspected = False
                        self.network_data_exhausted = False
                model_data = dict(outcome.data)
                if self.collection_candidate_ids:
                    model_data.update(self._dom_fallback_data())
                return ToolExecutionResult(
                    call.call_id,
                    call.name,
                    outcome.success,
                    outcome.message,
                    idempotent=outcome.idempotent,
                    evidence=outcome.evidence,
                    data=model_data,
                    counts_as_action=outcome.counts_as_action,
                )
            if call.name == capability_tools.CAPABILITY_GAP_TOOL_NAME:
                report = capability_tools.build_capability_gap_report(
                    call.arguments,
                    self.task,
                )
                self.capability_gap_reported = True
                return ToolExecutionResult(
                    call.call_id,
                    call.name,
                    True,
                    report.message,
                    idempotent=True,
                    data=report.data,
                    counts_as_action=False,
                )
            if call.name in CRAWL_TOOL_NAMES:
                return await self._check_crawl_policy(call, observation)
            if call.name == "capture_annotated_screenshot":
                return await self._capture_annotated_screenshot(call, observation)
            if call.name == "inspect_collection_structure":
                return await self._inspect_collection(call)
            if call.name == "run_structured_extraction":
                return await self._run_structured_extraction(call, observation)
            if call.name == "replay_collection_program":
                return await self._replay_collection_program(call, observation)
            if call.name == "navigate":
                refusal = await self._apply_crawl_policy(str(call.arguments.get("url") or ""))
                if refusal is not None:
                    return self._crawl_refusal(call, refusal)
            command, candidate, input_key = self._build_command(call, observation)
            challenge_refresh = bool(
                command.expected and command.expected.kind == "challenge_refreshed"
            )
            if call.name == "input_generated_text":
                if candidate is None:
                    raise PolicyViolationError("模型生成文本输入缺少有效目标区域")
                (
                    generated_security_challenge,
                    evidence,
                    generated_security_signature,
                ) = await self._authorize_generated_text_input(
                    call,
                    observation,
                    candidate,
                    normalized_text=command.value or "",
                )
            requires_pre_action_evidence = self._requires_pre_action_evidence(command)
            if requires_pre_action_evidence:
                try:
                    path = await capture_masked_evidence(
                        self.driver,
                        self.task.inputs,
                        f"high-risk-drag-{self.high_risk_drag_attempts + 1}-before",
                    )
                except Exception as exc:
                    raise PolicyViolationError(
                        "高风险拖拽尝试前无法保存截图证据，已停止执行"
                    ) from exc
                evidence_kind = (
                    "security_challenge_before"
                    if command.security_challenge
                    else "high_risk_drag_before"
                )
                evidence = EvidenceRef(
                    evidence_id=f"high-risk-drag-{uuid.uuid4().hex}",
                    kind=evidence_kind,
                    path=str(path),
                    summary="高风险拖拽尝试前页面截图",
                )
            if call.name == "inspect_visual_region":
                async with mask_task_inputs(self.driver, self.task.inputs):
                    receipt = await self.driver.execute(command)
            else:
                receipt = await self.driver.execute(command)
            if requires_pre_action_evidence and (
                receipt.success
                or not receipt.outcome_known
                or receipt.data.get("input_dispatched") is True
            ):
                self.high_risk_drag_attempts += 1
                if command.security_challenge:
                    self.security_challenge_active = True
                    self.security_challenge_phase = "awaiting_result"
                    if (
                        command.visual_drag_signature
                        and command.visual_drag_signature
                        not in self.security_challenge_drag_strategies
                    ):
                        self.security_challenge_drag_strategies.append(
                            command.visual_drag_signature
                        )
            if generated_security_challenge and (
                receipt.success
                or not receipt.outcome_known
                or receipt.data.get("input_dispatched") is True
            ):
                self.high_risk_drag_attempts += 1
                self.security_challenge_active = True
                self.security_challenge_phase = "text_entered"
                if (
                    generated_security_signature
                    and generated_security_signature not in self.security_challenge_text_signatures
                ):
                    self.security_challenge_text_signatures.append(generated_security_signature)
            if not receipt.success:
                return ToolExecutionResult(
                    call.call_id,
                    call.name,
                    False,
                    receipt.message,
                    failure_kind=ToolFailureKind.ACTION,
                    idempotent=command.idempotent,
                    receipt=receipt,
                    evidence=evidence,
                    data=await self._failure_data(command, receipt),
                )
            if call.name not in {"wait", "wait_until", "screenshot", "inspect_visual_region"}:
                self._invalidate_page_bound_inspections()
            if (
                call.name == "click"
                and self.security_challenge_active
                and self.security_challenge_text_entered
            ):
                # 点击已经成功送达，后置校验失败也代表正在等待验证码结果。
                self.security_challenge_phase = "awaiting_result"
            if call.name == "inspect_visual_region":
                region_path = receipt.data.get("path")
                if isinstance(region_path, str) and region_path:
                    evidence = EvidenceRef(
                        evidence_id=f"visual-region-{uuid.uuid4().hex}",
                        kind="visual_region_observation",
                        path=region_path,
                        summary="模型请求的临时视觉区域放大图",
                    )
            if command.expected:
                verification = await self.driver.verify(command.expected)
                refresh_reloaded = False
                if challenge_refresh and not verification.success:
                    reload_receipt = await self.driver.execute(
                        ActionCommand(
                            uuid.uuid4().hex,
                            ActionKind.NAVIGATE,
                            url=observation.url,
                            idempotent=True,
                        )
                    )
                    if reload_receipt.success:
                        verification = await self.driver.verify(command.expected)
                        refresh_reloaded = verification.success
                if not verification.success:
                    return ToolExecutionResult(
                        call.call_id,
                        call.name,
                        False,
                        f"动作已发出，但业务校验失败：{verification.reason}",
                        failure_kind=ToolFailureKind.VERIFICATION,
                        idempotent=command.idempotent,
                        receipt=receipt,
                        verification=verification,
                        evidence=evidence,
                        data=await self._failure_data(command, receipt),
                        counts_as_action=not challenge_refresh,
                    )
            if call.name not in {"wait", "wait_until", "screenshot", "inspect_visual_region"}:
                self.page_diagnostics_available = diagnostics.diagnostics_available(self.driver)
            result_data = self._visual_drag_audit(command, receipt) or dict(receipt.data)
            if command.expected and command.expected.kind == "challenge_refreshed":
                result_data["challenge_reload_fallback"] = refresh_reloaded
            return ToolExecutionResult(
                call.call_id,
                call.name,
                True,
                "动作执行并通过业务校验",
                idempotent=command.idempotent,
                receipt=receipt,
                verification=verification,
                plan_step=(
                    None
                    if call.name == "input_generated_text"
                    or call.name in locator_tools.LOCATOR_ACTION_TOOL_NAMES
                    else self._build_plan_step(command, candidate, input_key)
                ),
                evidence=evidence,
                data=result_data,
                counts_as_action=not challenge_refresh,
            )
        except RepeatedChallengeStrategyError as exc:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                str(exc),
                failure_kind=ToolFailureKind.POLICY,
                data={
                    "reason": "repeated_challenge_strategy",
                    "failed_drag_strategies": list(self.security_challenge_drag_strategies),
                    "failed_text_attempt_count": len(self.security_challenge_text_signatures),
                    "available_motion_profiles": list(VISUAL_DRAG_MOTION_PROFILES),
                    "available_geometry_modes": list(_VISUAL_DRAG_GEOMETRY_MODES),
                },
                counts_as_action=False,
            )
        except PolicyViolationError as exc:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                str(exc),
                failure_kind=ToolFailureKind.POLICY,
            )
        except (ValueError, TargetNotFoundError) as exc:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                str(exc),
                failure_kind=ToolFailureKind.REQUEST,
            )
        except RpaError as exc:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                f"浏览器或外部能力失败：{exc}",
                failure_kind=ToolFailureKind.INFRASTRUCTURE,
            )
        except Exception as exc:
            if (
                command is not None
                and not command.idempotent
                and (
                    receipt is None
                    or not receipt.outcome_known
                    or (command.expected is not None and verification is None)
                )
            ):
                unknown_receipt = receipt or ActionReceipt(
                    command.action_id,
                    False,
                    False,
                    "非幂等动作执行期间发生内部异常，业务结果未知",
                    0,
                )
                logger.exception(
                    "非幂等工具动作发生内部异常，结果未知",
                    extra={"tool": call.name, "exception_type": type(exc).__name__},
                )
                return ToolExecutionResult(
                    call.call_id,
                    call.name,
                    False,
                    "非幂等动作结果未知；禁止自动重放",
                    failure_kind=ToolFailureKind.ACTION,
                    idempotent=False,
                    receipt=unknown_receipt,
                )
            frames = tuple(
                {
                    "file": frame.filename,
                    "line": frame.lineno,
                    "function": frame.name,
                }
                for frame in traceback.extract_tb(exc.__traceback__)[-8:]
            )
            logger.exception(
                "工具执行发生内部代码异常",
                extra={"tool": call.name, "exception_type": type(exc).__name__},
            )
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                "工具执行发生内部代码异常；本轮已隔离该工具，请改用其余现有工具",
                failure_kind=ToolFailureKind.TOOL_DEFECT,
                idempotent=command.idempotent if command is not None else True,
                receipt=receipt,
                verification=verification,
                data={"exception_type": type(exc).__name__, "frames": frames},
            )

    async def _inspect_collection(self, call: ModelToolCall) -> ToolExecutionResult:
        if self.structured_extractor is None:
            raise PolicyViolationError("当前自动化表面不支持结构化数据采集")
        if call.arguments:
            raise ValueError("集合结构观察不接受模型生成的选择器或数量参数")
        max_candidates = 12
        structural_result = await self._cache_collection_inspection(max_candidates=max_candidates)
        safe_result = redact_task_inputs(structural_result, self.task.inputs)
        return ToolExecutionResult(
            call_id=call.call_id,
            name=call.name,
            success=True,
            message="集合结构已由代码完成只读分析，请只选择 candidate_id 后执行采集",
            idempotent=True,
            data=safe_result if isinstance(safe_result, dict) else {},
        )

    async def _cache_collection_inspection(self, *, max_candidates: int = 12) -> dict[str, Any]:
        if self.structured_extractor is None:
            return {"candidates": []}
        result = await self.structured_extractor.inspect(
            root_selector="body",
            max_candidates=max_candidates,
        )
        structural_result = sanitize_collection_inspection(
            result,
            max_candidates=max_candidates,
        )
        self.collection_inspection = structural_result
        self.collection_inspected = True
        return structural_result

    def _invalidate_page_bound_inspections(self) -> None:
        """页面动作后废弃旧观察，但保留同接口分页候选供下一轮聚合。"""

        self._network_candidates_before_page_change.update(self.network_candidate_ids)
        self.network_inspection = None
        self.network_data_inspected = False
        self.network_data_exhausted = False
        self.collection_inspection = None
        self.collection_inspected = False
        self.latest_extraction_result = None
        self.latest_extraction_spec = None
        self.latest_extraction_entry_url = None
        self.latest_network_extraction_result = None

    def _filter_network_candidates_after_page_change(
        self,
        candidates: list[Any],
    ) -> None:
        baseline = self._network_candidates_before_page_change
        if not baseline:
            return
        fresh = [
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping) and candidate.get("candidate_id") not in baseline
        ]
        fresh_signatures = {
            signature
            for candidate in fresh
            if (signature := self._network_candidate_signature(candidate)) is not None
        }
        candidates[:] = [
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping)
            and (
                candidate.get("candidate_id") not in baseline
                or self._network_candidate_signature(candidate) in fresh_signatures
            )
        ]
        baseline.clear()

    @staticmethod
    def _network_candidate_signature(
        candidate: Mapping[str, Any],
    ) -> tuple[str, tuple[str, ...]] | None:
        endpoint = candidate.get("endpoint")
        shape = candidate.get("json_shape")
        record_path = shape.get("record_path") if isinstance(shape, Mapping) else None
        if not isinstance(endpoint, str):
            return None
        return (
            endpoint,
            tuple(str(item) for item in record_path) if isinstance(record_path, list) else (),
        )

    def _network_batch_selection_error(self, arguments: Mapping[str, Any]) -> str | None:
        raw_ids = arguments.get("candidate_ids")
        if not isinstance(raw_ids, list) or len(raw_ids) < 2:
            return None
        candidates = (
            self.network_inspection.get("candidates", []) if self.network_inspection else []
        )
        by_id = {
            str(candidate["candidate_id"]): candidate
            for candidate in candidates
            if isinstance(candidate, Mapping) and isinstance(candidate.get("candidate_id"), str)
        }
        selected = [by_id.get(str(candidate_id)) for candidate_id in raw_ids]
        if any(candidate is None for candidate in selected):
            return None
        endpoints = {str(candidate.get("endpoint", "")) for candidate in selected if candidate}
        record_paths = {
            tuple(shape.get("record_path", []))
            for candidate in selected
            if candidate
            and isinstance((shape := candidate.get("json_shape")), Mapping)
            and isinstance(shape.get("record_path"), list)
        }
        if len(endpoints) == 1 and len(record_paths) == 1 and next(iter(record_paths), ()):
            return None
        return "所选网络候选不属于同一接口和记录结构，已停止重复批量导出并切换到 DOM 结构化采集"

    def _dom_fallback_data(self) -> dict[str, Any]:
        if not self.collection_candidate_ids:
            return {}
        return {
            "dom_fallback": {
                "status": "ready",
                "candidate_ids": list(self.collection_candidate_ids),
                "instruction": "网络响应不完整；下一步从候选 ID 中选择并批量采集",
            }
        }

    async def _failure_data(self, command: ActionCommand, receipt: ActionReceipt) -> dict[str, Any]:
        data = await diagnostics.enrich_failure_data(
            self.driver, self.task.inputs, self._visual_drag_audit(command, receipt)
        )
        if command.security_challenge:
            # 验证码失败由引擎自己的次数预算与刷新状态机处理，避免并发监督打断恢复。
            data["security_challenge"] = True
            data["recovery_managed_by_engine"] = True
        return data

    async def _run_structured_extraction(
        self,
        call: ModelToolCall,
        observation: Observation,
    ) -> ToolExecutionResult:
        if self.structured_extractor is None:
            raise PolicyViolationError("当前自动化表面不支持结构化数据采集")
        if not self.driver.capabilities.dom or not self.driver.capabilities.javascript:
            raise PolicyViolationError("当前自动化表面缺少 DOM 或固定脚本采集能力")
        if "candidate_id" in call.arguments:
            if self.collection_inspection is None:
                raise ValueError("集合结构观察缓存已失效，请重新观察")
            spec = collection_spec_from_inspection(
                call.arguments,
                self.collection_inspection,
                require_details=requires_record_details(self.task.goal),
            )
        else:
            # 兼容已保存的旧执行计划；当前模型 schema 只开放短候选引用协议。
            spec = CollectionExtractionSpec.from_mapping(call.arguments)
        result = await self.structured_extractor.extract(spec)
        self.latest_extraction_result = result
        # 入口取本轮观察 URL：规格在该页面上被验证，验证门要从同一入口重放。
        self.latest_extraction_spec = spec
        self.latest_extraction_entry_url = observation.url
        if result.interrupted_by_security_challenge:
            # 详情采集把页面留在新挑战上，下一轮必须重新开放视觉工具；
            # 旧列表候选在挑战清除并返回列表前已经失效。
            self.collection_inspected = False
            self.collection_inspection = None
        safe_summary = redact_task_inputs(result.model_summary(), self.task.inputs)
        evidence = None
        if result.json_path is not None:
            evidence = EvidenceRef(
                evidence_id=f"structured-data-{uuid.uuid4().hex}",
                kind="structured_data_json",
                path=str(result.json_path),
                summary=(f"{result.collection_name}代码采集结果，共 {result.exported_count} 条"),
            )
        strong = result.complete and result.has_strong_completion_evidence
        if strong and evidence is not None:
            await self._promote_latest_collection_program(evidence.evidence_id)
        return ToolExecutionResult(
            call_id=call.call_id,
            name=call.name,
            success=strong,
            message=(
                (
                    "结构化数据已由代码完成全部分页、逐条详情采集、校验和导出"
                    if result.detail_requested
                    else "结构化数据已由代码完成全部分页采集、校验和导出"
                )
                if strong
                else (
                    "详情采集遇到新的安全挑战，页面已保留并切换视觉处理"
                    if result.interrupted_by_security_challenge
                    else "结构化数据代码采集未通过完整性校验，请调整规格后重试"
                )
            ),
            failure_kind=(None if strong else ToolFailureKind.VERIFICATION),
            idempotent=False,
            evidence=evidence,
            data=safe_summary if isinstance(safe_summary, dict) else {},
        )

    async def _check_crawl_policy(
        self,
        call: ModelToolCall,
        observation: Observation,
    ) -> ToolExecutionResult:
        try:
            outcome = await execute_check_crawl_policy(
                call.arguments,
                driver=self.driver,
                store=self.crawl_policies,
                current_url=observation.url,
            )
        except ValueError as exc:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                str(exc),
                failure_kind=ToolFailureKind.REQUEST,
                idempotent=True,
                counts_as_action=False,
            )
        return ToolExecutionResult(
            call.call_id,
            call.name,
            outcome.success,
            outcome.message,
            idempotent=True,
            counts_as_action=False,
            data=outcome.data,
            model_data=outcome.model_data,
        )

    def _crawl_refusal(self, call: ModelToolCall, reason: str) -> ToolExecutionResult:
        return ToolExecutionResult(
            call.call_id,
            call.name,
            False,
            reason,
            failure_kind=ToolFailureKind.POLICY,
            idempotent=True,
            counts_as_action=False,
            data={"blocked_by": "robots.txt"},
        )

    async def _apply_crawl_policy(self, url: str) -> str | None:
        """导航前的抓取策略闸门；返回拒绝原因，None 表示放行。

        只有显式打开 `respect_robots` 才会拦截：robots.txt 约束自动化抓取，而本库也用于
        登录自家系统这类交互场景，默认拦下会把正当用途一起挡掉。放行前按主机节奏等待。
        """

        if self.respect_robots:
            try:
                origin = origin_of(url)
            except ValueError:
                return None
            policy = self.crawl_policies.get(origin)
            if policy is None:
                # 打开遵守开关后首次访问某站点自动取一次，调用方不必记着先查。
                policy = await self.crawl_policies.load(self.driver, origin)
            allowed, rule = policy.decide(url)
            if allowed is False:
                pattern = rule.pattern if rule is not None else ""
                return f"robots.txt 禁止抓取该地址，命中规则 {pattern}，已按遵守设置停止导航"
            if allowed is None:
                return f"robots.txt 状态未知：{policy.reason}；遵守设置下不放行"
        if self.crawl_pacer is not None:
            await self.crawl_pacer.acquire(url)
        return None

    async def _capture_annotated_screenshot(
        self,
        call: ModelToolCall,
        observation: Observation,
    ) -> ToolExecutionResult:
        """把当前观察的候选编号画到截图上，并回带编号到 target_id 的图例。"""

        if not self.driver.capabilities.javascript:
            raise PolicyViolationError("当前自动化表面不支持在页面上绘制标注覆盖层")
        labels = build_annotation_labels(
            observation.candidates,
            max_labels=int(call.arguments.get("max_labels") or DEFAULT_MAX_LABELS),
            roles=tuple(call.arguments.get("roles") or ()),
        )
        if not labels:
            return ToolExecutionResult(
                call_id=call.call_id,
                name=call.name,
                success=False,
                message=(
                    "当前观察没有带可见矩形的候选可标注；请先滚动或等待页面渲染，或改用显式定位器"
                ),
                idempotent=True,
                counts_as_action=False,
                data={"candidate_count": len(observation.candidates), "legend": []},
            )
        result = await self.driver.capture_annotated_screenshot(
            labels,
            label=str(call.arguments.get("label") or "annotated"),
        )
        drawn = set(result.get("drawn") or ())
        # 完全在视口外的候选不会被画上去，图例只保留图上真实可见的编号。
        legend = [item.public_dict() for item in labels if item.label in drawn]
        evidence = EvidenceRef(
            evidence_id=f"annotated-screenshot-{uuid.uuid4().hex}",
            kind="annotated_screenshot",
            path=str(result["screenshot_path"]),
            summary=f"带编号标注的视口截图，共 {len(legend)} 个候选",
        )
        return ToolExecutionResult(
            call_id=call.call_id,
            name=call.name,
            success=bool(legend),
            message=(
                f"已标注 {len(legend)} 个候选并截图；按图例编号取 target_id 操作"
                if legend
                else "候选全部位于视口之外，截图里没有可用编号"
            ),
            idempotent=True,
            counts_as_action=False,
            evidence=evidence,
            data={
                "screenshot_path": result["screenshot_path"],
                "legend": legend,
                "candidate_count": len(observation.candidates),
                "annotated_count": len(legend),
            },
            model_data={
                "legend": [
                    {
                        "label": item["label"],
                        "target_id": item["target_id"],
                        "role": item["role"],
                        "name": item["name"],
                    }
                    for item in legend
                ],
                "candidate_count": len(observation.candidates),
                "annotated_count": len(legend),
                "note": "截图路径只返回给调用方进程",
            },
        )

    async def emit_program_event(
        self,
        kind: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """采集程序晋升/拒绝事件：工具库路径只记日志，不依赖智能体事件总线。"""

        logger.info(
            message,
            extra={
                "task_id": self.task.task_id,
                "event_kind": kind,
                **(data or {}),
            },
        )

    async def _promote_latest_collection_program(self, evidence_id: str) -> None:
        if (
            self.latest_extraction_spec is None
            or self.latest_extraction_entry_url is None
            or self.latest_extraction_result is None
        ):
            return
        await verify_and_promote_collection_program(
            self,
            self.task,
            self.latest_extraction_spec,
            self.latest_extraction_entry_url,
            self.latest_extraction_result,
            evidence_id,
        )

    async def _replay_collection_program(
        self,
        call: ModelToolCall,
        observation: Observation,
    ) -> ToolExecutionResult:
        if self.structured_extractor is None:
            raise PolicyViolationError("当前自动化表面不支持结构化数据采集")
        if self.memory_runtime is None:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                "未装配采集程序库，无法重放；请先用 inspect_collection_structure 与 "
                "run_structured_extraction 编译规格",
                idempotent=True,
                counts_as_action=False,
                data={"fallback": "inspect_collection_structure"},
            )
        await self.memory_runtime.flush(timeout_seconds=5.0)
        program = await self.memory_runtime.await_best_collection_program(
            scope=self.task.scope,
            scenario_key=scenario_key(self.task.goal),
            url=sanitize_url_for_storage(observation.url, self.task.inputs),
        )
        if program is None:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                "当前场景没有已验证采集程序，请先检查结构并提交规格",
                idempotent=True,
                counts_as_action=False,
                data={"fallback": "inspect_collection_structure"},
            )
        try:
            spec = CollectionExtractionSpec.from_mapping(program.spec)
        except ValueError as exc:
            self.memory_runtime.record_collection_program_outcome_later(
                program.program_id,
                success=False,
                latency_ms=0.0,
            )
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                f"采集程序规格已失效，回退重新编译：{exc}",
                idempotent=True,
                counts_as_action=False,
                data={"program_id": program.program_id, "fallback": "inspect_collection_structure"},
            )
        if requires_record_details(self.task.goal) and spec.detail_trigger_selector is None:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                "已验证程序不含详情链路，无法满足当前任务的详情要求",
                idempotent=True,
                counts_as_action=False,
                data={"program_id": program.program_id, "fallback": "inspect_collection_structure"},
            )
        probe_reason = await probe_entry_until_ready(
            self.structured_extractor,
            spec,
            timeout_seconds=spec.page_wait_timeout_seconds,
        )
        if probe_reason is not None:
            self.memory_runtime.record_collection_program_outcome_later(
                program.program_id,
                success=False,
                latency_ms=0.0,
            )
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                f"采集程序入口结构失配，回退重新编译：{probe_reason}",
                idempotent=True,
                counts_as_action=False,
                data={
                    "program_id": program.program_id,
                    "reason": probe_reason,
                    "fallback": "inspect_collection_structure",
                },
            )
        started = asyncio.get_running_loop().time()
        result = await self.structured_extractor.extract(spec)
        latency_ms = (asyncio.get_running_loop().time() - started) * 1000
        self.latest_extraction_result = result
        self.latest_extraction_spec = spec
        self.latest_extraction_entry_url = observation.url
        if result.interrupted_by_security_challenge:
            # 安全挑战不是程序结构问题，不降权。
            return ToolExecutionResult(
                call.call_id,
                call.name,
                False,
                "采集程序重放遇到安全挑战，请先处理挑战后再重试",
                idempotent=False,
                counts_as_action=True,
                data={"program_id": program.program_id, "security_challenge": True},
            )
        strong = result.complete and result.has_strong_completion_evidence
        self.memory_runtime.record_collection_program_outcome_later(
            program.program_id,
            success=strong,
            latency_ms=latency_ms,
        )
        safe_summary = redact_task_inputs(result.model_summary(), self.task.inputs)
        evidence = None
        if result.json_path is not None:
            evidence = EvidenceRef(
                evidence_id=f"structured-data-{uuid.uuid4().hex}",
                kind="structured_data_json",
                path=str(result.json_path),
                summary=(f"{result.collection_name}程序重放结果，共 {result.exported_count} 条"),
            )
        data = dict(safe_summary) if isinstance(safe_summary, dict) else {}
        data["program_id"] = program.program_id
        data["replay"] = True
        return ToolExecutionResult(
            call.call_id,
            call.name,
            strong,
            (
                "已验证采集程序零模型重放成功"
                if strong
                else "采集程序重放未通过完整性校验，请回退重新编译规格"
            ),
            failure_kind=None if strong else ToolFailureKind.VERIFICATION,
            idempotent=False,
            evidence=evidence,
            data=data,
        )

    def _build_command(
        self,
        call: ModelToolCall,
        observation: Observation,
    ) -> tuple[ActionCommand, CandidateTarget | None, str | None]:
        arguments = call.arguments
        action_id = uuid.uuid4().hex
        candidate: CandidateTarget | None = None
        input_key: str | None = None
        # 两类文本输入都由浏览器驱动回读真实 value 校验；不让模型拼装无法证明写入值的后置条件。
        expected = (
            None
            if call.name
            in {
                "input_text",
                "input_generated_text",
                "input_text_locator",
                "upload_files",
            }
            else self._expected(arguments)
        )
        if call.name in {"click", "click_locator"} and expected is not None:
            expected = replace(expected, timeout_seconds=CLICK_VERIFICATION_TIMEOUT_SECONDS)
            if self.security_challenge_text_entered and expected.kind == "fingerprint_changed":
                # 当前观察指纹由执行层持有。验证码提交阶段允许模型选择按钮，
                # 但不让它复制或复用这个动态校验字段。
                expected = replace(expected, value=observation.fingerprint)
        challenge_expected = drag_challenge_condition(
            call.name,
            arguments.get("target_id"),
            observation,
        )
        if challenge_expected is not None:
            expected = challenge_expected
        if (
            expected is not None
            and call.name in {"select", "drag", "visual_click", "visual_drag", "press_key"}
            and condition_visible_before_action(expected, observation)
        ):
            raise PolicyViolationError("业务后置条件在动作前已经满足，不能用于证明动作成功")
        if (
            expected is not None
            and expected.kind == "fingerprint_changed"
            and expected.value != observation.fingerprint
        ):
            raise PolicyViolationError("页面变化校验必须绑定当前观察 fingerprint")
        if call.name == "navigate":
            url = self._required_string(arguments, "url")
            assert_navigation_allowed(self.task, url)
            command = ActionCommand(
                action_id,
                ActionKind.NAVIGATE,
                url=url,
                expected=ExpectedCondition("url_contains", url, 20),
                idempotent=True,
            )
        elif call.name in locator_tools.LOCATOR_ACTION_TOOL_NAMES:
            command, input_key = locator_tools.build_locator_command(
                call,
                self.task,
                action_id,
                expected,
            )
        elif call.name in element_tools.PAGE_CONTROL_TOOL_NAMES:
            command = element_tools.build_page_control_command(call, action_id, expected)
        elif call.name in element_tools.POINTER_TOOL_NAMES:
            command = element_tools.build_pointer_command(call, action_id, expected)
        elif call.name == "upload_files":
            command, _ = file_tools.build_upload_command(
                call,
                task=self.task,
                action_id=action_id,
                observation_fingerprint=observation.fingerprint,
            )
        elif call.name in {"click", "input_text", "input_generated_text", "select", "drag"}:
            target_id = self._required_string(arguments, "target_id")
            candidate = self._candidate(observation, target_id)
            if candidate.disabled:
                raise PolicyViolationError("目标区域处于禁用状态")
            if candidate.confidence < 0.6:
                raise PolicyViolationError("目标定位置信度过低，需要重新观察")
            refresh_expected = challenge_refresh_condition(candidate, observation)
            if call.name == "click" and refresh_expected is not None:
                expected = refresh_expected
            if (
                call.name == "click"
                and expected is not None
                and condition_visible_before_action(expected, observation)
            ):
                expected = read_only_click_fallback_condition(candidate, observation)
                if expected is None:
                    raise PolicyViolationError("业务后置条件在动作前已经满足，不能用于证明动作成功")
            if expected is None and call.name not in {"input_text", "input_generated_text"}:
                raise PolicyViolationError("有副作用动作必须提供业务后置条件")
            if expected is not None and expected.kind == "target_exists":
                self._candidate(observation, expected.value)
            if call.name == "drag" and expected is not None and expected.kind == "target_exists":
                raise PolicyViolationError("拖拽动作不能只用目标仍然存在作为成功条件")
            if call.name == "click":
                button, click_count = resolve_pointer(
                    arguments.get("button"), arguments.get("click_count")
                )
                command = ActionCommand(
                    action_id,
                    ActionKind.CLICK,
                    target_id=target_id,
                    expected=expected,
                    # 右键与双击不改变"是否只读"的判定，但都不可当作可重放的幂等动作。
                    idempotent=is_read_only_click(candidate)
                    and button == "left"
                    and click_count == 1,
                    pointer_button=button,
                    click_count=click_count,
                )
            elif call.name == "input_text":
                try:
                    value, input_key = locator_tools.resolve_text_input(arguments, self.task)
                except ValueError as exc:
                    raise PolicyViolationError(str(exc)) from exc
                command = ActionCommand(
                    action_id,
                    ActionKind.INPUT_TEXT,
                    target_id=target_id,
                    value=value,
                    expected=expected,
                    idempotent=True,
                )
            elif call.name == "input_generated_text":
                value = self._required_string(arguments, "text")
                value = (
                    "".join(value.split())
                    if arguments.get("security_challenge") is True
                    else value.strip()
                )
                if not value:
                    raise PolicyViolationError("模型生成文本不能为空")
                if len(value) > 128:
                    raise PolicyViolationError("模型生成文本不能超过 128 个字符")
                if any(ord(char) < 32 or ord(char) == 127 for char in value):
                    raise PolicyViolationError("模型生成文本不能包含控制字符")
                command = ActionCommand(
                    action_id,
                    ActionKind.INPUT_TEXT,
                    target_id=target_id,
                    value=value,
                    expected=expected,
                    idempotent=True,
                )
            elif call.name == "select":
                raw_input_key = arguments.get("input_key")
                if raw_input_key is not None:
                    input_key = str(raw_input_key)
                    if input_key not in self.task.inputs:
                        raise PolicyViolationError(f"任务输入键不存在：{input_key}")
                    value = str(self.task.inputs[input_key])
                else:
                    value = self._required_string(arguments, "value")
                command = ActionCommand(
                    action_id,
                    ActionKind.SELECT,
                    target_id=target_id,
                    value=value,
                    expected=expected,
                    idempotent=True,
                )
            else:
                challenge = self._authorize_drag_risk(
                    arguments,
                    candidate.drag_risk,
                    observation.url,
                    visual=False,
                )
                end_dx = self._required_number(arguments, "end_dx")
                end_dy = self._required_number(arguments, "end_dy")
                duration_ms = self._required_integer(arguments, "duration_ms")
                steps = self._required_integer(arguments, "steps")
                trajectory = build_drag_trajectory(
                    end_dx=end_dx,
                    end_dy=end_dy,
                    duration_ms=duration_ms,
                    steps=steps,
                )
                drag_strategy = None
                if challenge:
                    drag_strategy = hashlib.sha256(
                        json.dumps(
                            {
                                "observation": observation.fingerprint,
                                "target": target_id,
                                "end_dx": round(end_dx, 3),
                                "end_dy": round(end_dy, 3),
                                "duration_ms": duration_ms,
                                "steps": steps,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    if drag_strategy in self.security_challenge_drag_strategies:
                        raise RepeatedChallengeStrategyError(
                            "当前页面上的等价语义拖拽策略已经执行失败；必须重新观察并实质修改"
                            "目标、位移、时长或轨迹点数"
                        )
                command = ActionCommand(
                    action_id,
                    ActionKind.DRAG,
                    target_id=target_id,
                    trajectory=trajectory,
                    visual_drag_strategy="semantic" if challenge else None,
                    visual_drag_signature=drag_strategy,
                    security_challenge=challenge,
                    drag_risk=candidate.drag_risk,
                    drag_risk_reasons=candidate.drag_risk_reasons,
                    expected=expected,
                    idempotent=False,
                )
        elif call.name == "inspect_visual_region":
            if not self.task.allow_visual_actions:
                raise PolicyViolationError("任务未授权视觉区域观察")
            if not self.visual_context_available:
                raise PolicyViolationError("当前模型图片输入未启用，不能放大观察视觉区域")
            fingerprint = self._required_string(arguments, "observation_fingerprint")
            if fingerprint != observation.fingerprint:
                raise PolicyViolationError("视觉区域观察绑定的页面观察已经失效")
            confidence = self._required_ratio(arguments, "visual_confidence")
            if confidence < 0.8:
                raise PolicyViolationError("视觉区域观察置信度低于 0.8，必须重新观察")
            command = ActionCommand(
                action_id,
                ActionKind.INSPECT_VISUAL_REGION,
                visual_clip=(
                    self._required_ratio(arguments, "x_ratio"),
                    self._required_ratio(arguments, "y_ratio"),
                    self._required_ratio(arguments, "width_ratio"),
                    self._required_ratio(arguments, "height_ratio"),
                ),
                observation_fingerprint=fingerprint,
                screenshot_fingerprint=self._required_string(
                    arguments,
                    "screenshot_fingerprint",
                ),
                visual_confidence=confidence,
                idempotent=True,
            )
        elif call.name == "visual_click":
            if not self.task.allow_visual_actions:
                raise PolicyViolationError("任务未授权视觉坐标动作")
            if not self.visual_context_available:
                raise PolicyViolationError("当前模型图片输入未启用，不能执行视觉坐标动作")
            if expected is None:
                raise PolicyViolationError("有副作用动作必须提供业务后置条件")
            if expected.kind == "target_exists":
                raise PolicyViolationError("视觉点击不能使用目标存在作为成功条件")
            fingerprint = self._required_string(arguments, "observation_fingerprint")
            if fingerprint != observation.fingerprint:
                raise PolicyViolationError("视觉点击绑定的页面观察已经失效")
            confidence = self._required_ratio(arguments, "visual_confidence")
            if confidence < 0.8:
                raise PolicyViolationError("视觉点击置信度低于 0.8，必须重新观察")
            command = ActionCommand(
                action_id,
                ActionKind.VISUAL_CLICK,
                visual_x_ratio=self._required_ratio(arguments, "x_ratio"),
                visual_y_ratio=self._required_ratio(arguments, "y_ratio"),
                observation_fingerprint=fingerprint,
                screenshot_fingerprint=self._required_string(
                    arguments,
                    "screenshot_fingerprint",
                ),
                visual_confidence=confidence,
                expected=expected,
                idempotent=False,
            )
        elif call.name == "visual_drag":
            if not self.task.allow_visual_actions:
                raise PolicyViolationError("任务未授权视觉坐标动作")
            if not self.visual_context_available:
                raise PolicyViolationError("当前模型图片输入未启用，不能执行视觉坐标动作")
            if expected is None:
                raise PolicyViolationError("有副作用动作必须提供业务后置条件")
            if expected.kind == "target_exists":
                raise PolicyViolationError("视觉拖拽不能使用目标存在作为成功条件")
            fingerprint = self._required_string(arguments, "observation_fingerprint")
            if fingerprint != observation.fingerprint:
                raise PolicyViolationError("视觉拖拽绑定的页面观察已经失效")
            screenshot_fingerprint = self._required_string(arguments, "screenshot_fingerprint")
            confidence = self._required_number(arguments, "visual_confidence")
            if not 0.8 <= confidence <= 1:
                raise PolicyViolationError("视觉拖拽置信度低于 0.8，必须重新观察")
            challenge = self._authorize_drag_risk(
                arguments,
                observation.visual_drag_risk,
                observation.url,
                visual=True,
            )
            duration_ms = self._required_integer(arguments, "duration_ms")
            steps = self._required_integer(arguments, "steps")
            motion_profile = str(arguments.get("motion_profile", "balanced"))
            if motion_profile not in VISUAL_DRAG_MOTION_PROFILES:
                raise ValueError("视觉拖拽运动策略无效")
            requested_geometry_mode = str(arguments.get("geometry_mode", "track"))
            if requested_geometry_mode not in _VISUAL_DRAG_GEOMETRY_MODES:
                raise ValueError("视觉拖拽几何策略无效")
            model_geometry = (
                self._required_ratio(arguments, "start_x_ratio"),
                self._required_ratio(arguments, "start_y_ratio"),
                self._required_ratio(arguments, "end_x_ratio"),
                self._required_ratio(arguments, "end_y_ratio"),
            )
            inferred_geometry = security_drag_geometry_ratios(observation)
            geometry_mode = requested_geometry_mode
            if geometry_mode == "track" and inferred_geometry is None:
                geometry_mode = "model"
            if geometry_mode == "model" and inferred_geometry is not None:
                geometry_error = security_drag_geometry_error(arguments, observation)
                if geometry_error is not None:
                    raise PolicyViolationError(geometry_error)
            geometry = (
                inferred_geometry
                if geometry_mode == "track" and inferred_geometry is not None
                else model_geometry
            )
            if geometry_mode == "track" and geometry != model_geometry:
                logger.info(
                    "视觉滑块坐标已按页面轨道几何校准",
                    extra={"task_id": self.task.task_id},
                )
            drag_strategy_label = f"{geometry_mode}:{motion_profile}"
            drag_strategy_payload = {
                "screenshot": screenshot_fingerprint,
                "observation": fingerprint,
                "geometry_mode": geometry_mode,
                "geometry": [round(value, 6) for value in geometry],
                "motion_profile": motion_profile,
                "duration_ms": duration_ms,
                "steps": steps,
            }
            drag_strategy = hashlib.sha256(
                json.dumps(
                    drag_strategy_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if challenge and drag_strategy in self.security_challenge_drag_strategies:
                raise RepeatedChallengeStrategyError(
                    "当前页面和截图上的等价拖拽策略已经执行失败；必须基于最新观察实质修改"
                    "几何、运动模式、时长或轨迹点数，不能再次提交相同方案"
                )
            visual_trajectory = build_human_visual_drag_trajectory(
                start_x_ratio=geometry[0],
                start_y_ratio=geometry[1],
                end_x_ratio=geometry[2],
                end_y_ratio=geometry[3],
                duration_ms=duration_ms,
                steps=steps,
                seed=action_id,
                motion_profile=motion_profile,
            )
            command = ActionCommand(
                action_id,
                ActionKind.VISUAL_DRAG,
                visual_trajectory=visual_trajectory,
                observation_fingerprint=fingerprint,
                screenshot_fingerprint=screenshot_fingerprint,
                visual_confidence=confidence,
                visual_drag_strategy=drag_strategy_label,
                visual_drag_signature=drag_strategy,
                allow_dynamic_visual_frame=(
                    challenge and observation.visual_drag_risk is DragRiskClass.SECURITY
                ),
                security_challenge=challenge,
                drag_risk=observation.visual_drag_risk,
                drag_risk_reasons=observation.visual_drag_risk_reasons,
                expected=expected,
                idempotent=False,
            )
            logger.info(
                "视觉验证码策略已绑定当前观察和轨迹参数",
                extra={"task_id": self.task.task_id, "strategy": drag_strategy_label},
            )
        elif call.name == "scroll":
            amount = float(arguments.get("amount", 0))
            if abs(amount) > 5000:
                raise PolicyViolationError("单次滚动距离不能超过 5000 像素")
            command = ActionCommand(
                action_id,
                ActionKind.SCROLL,
                value=str(amount),
                idempotent=True,
            )
        elif call.name == "wait":
            seconds = float(arguments.get("seconds", 0))
            if seconds < 0 or seconds > 10:
                raise PolicyViolationError("单次等待必须在 0 到 10 秒之间")
            command = ActionCommand(
                action_id,
                ActionKind.WAIT,
                value=str(seconds),
                idempotent=True,
            )
        elif call.name == "screenshot":
            label = self._required_string(arguments, "label")
            command = ActionCommand(
                action_id,
                ActionKind.SCREENSHOT,
                value=label,
                idempotent=True,
            )
        else:
            raise PolicyViolationError(f"模型请求了未授权工具：{call.name}")

        if command.kind not in self.task.allowed_actions:
            raise PolicyViolationError(f"任务未授权动作：{command.kind.value}")
        return command, candidate, input_key

    async def _authorize_generated_text_input(
        self,
        call: ModelToolCall,
        observation: Observation,
        candidate: CandidateTarget,
        *,
        normalized_text: str,
    ) -> tuple[bool, EvidenceRef, str | None]:
        """只允许模型把当前截图中识别出的短文本输入非密码字段。"""
        if not self.visual_context_available:
            raise PolicyViolationError("当前模型图片输入未启用，不能输入模型识别文本")
        if not self.task.allow_visual_actions:
            raise PolicyViolationError("任务未授权使用多模态视觉输入结果")
        if candidate.role not in {"textbox", "searchbox", "spinbutton"}:
            raise PolicyViolationError("模型生成文本只能输入文本类目标区域")
        if self._candidate_input_type(candidate) == "password":
            raise PolicyViolationError("模型生成文本不得输入密码框")

        arguments = call.arguments
        observation_fingerprint = self._required_string(
            arguments,
            "observation_fingerprint",
        )
        if observation_fingerprint != observation.fingerprint:
            raise PolicyViolationError("模型生成文本绑定的页面观察已经失效")
        confidence = self._required_ratio(arguments, "visual_confidence")
        if confidence < 0.8:
            raise PolicyViolationError("模型生成文本的视觉置信度低于 0.8")

        declared_challenge = arguments.get("security_challenge")
        if not isinstance(declared_challenge, bool):
            raise ValueError("工具参数 security_challenge 必须是布尔值")
        page_is_challenge = observation.visual_drag_risk is DragRiskClass.SECURITY
        if page_is_challenge and not declared_challenge:
            raise PolicyViolationError("页面疑似安全挑战，模型不得把验证码降级为普通文本")
        if declared_challenge:
            page_origin = normalize_url(observation.url).origin
            if not self.task.allows_security_challenge_at(page_origin):
                raise PolicyViolationError("任务未授权处理安全挑战")
        expected_screenshot = self._required_string(arguments, "screenshot_fingerprint")
        strategy_signature = None
        if declared_challenge:
            strategy_signature = hashlib.sha256(
                f"{observation.fingerprint}\0{expected_screenshot}\0{normalized_text}".encode()
            ).hexdigest()
            if strategy_signature in self.security_challenge_text_signatures:
                raise RepeatedChallengeStrategyError(
                    "同一验证码截图下的相同答案已经提交失败；必须重新观察图片并生成不同答案"
                )
        try:
            path = await capture_masked_evidence(
                self.driver,
                self.task.inputs,
                f"model-generated-text-{self.high_risk_drag_attempts + 1}-before",
            )
            image_bytes = await asyncio.to_thread(path.read_bytes)
        except Exception as exc:
            raise PolicyViolationError("模型生成文本输入前无法保存当前截图，已停止执行") from exc
        current_screenshot = hashlib.sha256(image_bytes).hexdigest()
        if current_screenshot != expected_screenshot:
            raise PolicyViolationError("模型识别文本所依据的截图已经变化，必须重新观察")
        return (
            declared_challenge,
            EvidenceRef(
                evidence_id=f"model-generated-text-{uuid.uuid4().hex}",
                kind="model_generated_text_before",
                path=str(path),
                summary="模型生成短文本输入前的当前页面截图",
            ),
            strategy_signature,
        )

    @staticmethod
    def _candidate_input_type(candidate: CandidateTarget) -> str:
        try:
            locator = json.loads(candidate.recipe.value or "{}")
        except json.JSONDecodeError:
            return ""
        if not isinstance(locator, dict):
            return ""
        attributes = locator.get("attrs")
        if not isinstance(attributes, dict):
            return ""
        input_type = attributes.get("type")
        return str(input_type).casefold() if input_type is not None else ""

    @staticmethod
    def _candidate(observation: Observation, target_id: str) -> CandidateTarget:
        for candidate in observation.candidates:
            if candidate.target_id == target_id:
                return candidate
        raise TargetNotFoundError("目标区域不属于当前页面观察，请重新观察")

    @staticmethod
    def _expected(arguments: dict[str, Any]) -> ExpectedCondition | None:
        kind = arguments.get("expect_kind")
        value = arguments.get("expect_value")
        if kind is None and value is None:
            return None
        if not isinstance(kind, str) or not isinstance(value, str) or not kind or not value:
            raise ValueError("业务后置条件必须同时提供类型和值")
        if kind not in SUPPORTED_EXPECTED_KINDS:
            raise ValueError(f"不支持的业务后置条件：{kind}")
        return ExpectedCondition(kind, value)

    @staticmethod
    def _required_string(arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"工具参数 {key} 必须是非空字符串")
        return value

    @staticmethod
    def _required_number(arguments: dict[str, Any], key: str) -> float:
        value = arguments.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"工具参数 {key} 必须是数值")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"工具参数 {key} 必须是有限数值")
        if abs(number) > 3000:
            raise ValueError(f"工具参数 {key} 不能超过 3000 像素")
        return number

    @staticmethod
    def _required_integer(arguments: dict[str, Any], key: str) -> int:
        value = arguments.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"工具参数 {key} 必须是整数")
        return value

    def _authorize_drag_risk(
        self,
        arguments: dict[str, Any],
        risk: DragRiskClass,
        page_url: str,
        *,
        visual: bool,
    ) -> bool:
        declared_challenge = arguments.get("security_challenge")
        if not isinstance(declared_challenge, bool):
            raise ValueError("工具参数 security_challenge 必须是布尔值")
        allowed_risks = (
            self.task.allowed_visual_drag_risks if visual else self.task.allowed_drag_risks
        )
        page_origin = normalize_url(page_url).origin
        security_allowed = self.task.allows_security_challenge_at(page_origin)
        if risk not in allowed_risks and not (risk is DragRiskClass.SECURITY and security_allowed):
            if risk is DragRiskClass.SECURITY:
                raise PolicyViolationError("页面疑似安全挑战，任务未授权处理安全挑战")
            if risk is DragRiskClass.UNKNOWN:
                if visual:
                    raise PolicyViolationError("视觉拖拽风险无法确认，任务未显式授权未知视觉拖拽")
                raise PolicyViolationError("语义拖拽风险无法确认，只允许可证明的普通业务滑块")
            raise PolicyViolationError("任务未授权当前拖拽风险类型")

        security_challenge = declared_challenge or risk is DragRiskClass.SECURITY
        if security_challenge and not security_allowed:
            raise PolicyViolationError("任务未授权处理安全挑战")
        return security_challenge

    @staticmethod
    def _is_high_risk_drag(risk: DragRiskClass, security_challenge: bool) -> bool:
        return security_challenge or risk is not DragRiskClass.BUSINESS

    @classmethod
    def _requires_pre_action_evidence(cls, command: ActionCommand) -> bool:
        if command.drag_risk is None:
            return False
        return cls._is_high_risk_drag(command.drag_risk, command.security_challenge)

    @staticmethod
    def _visual_drag_audit(
        command: ActionCommand,
        receipt: ActionReceipt,
    ) -> dict[str, Any]:
        """保留视觉拖拽的脱敏几何证据，便于失败后判断定位或轨迹问题。"""
        if command.kind is not ActionKind.VISUAL_DRAG or not command.visual_trajectory:
            return {}
        start = command.visual_trajectory[0]
        end = command.visual_trajectory[-1]
        delays = [point.delay_ms for point in command.visual_trajectory]
        vertical = [point.y_ratio for point in command.visual_trajectory]
        audit = {
            "执行策略": command.visual_drag_strategy or "未记录",
            "起点视口比例": {"x": start.x_ratio, "y": start.y_ratio},
            "终点视口比例": {"x": end.x_ratio, "y": end.y_ratio},
            "轨迹点数": len(command.visual_trajectory),
            "轨迹总时长毫秒": sum(point.delay_ms for point in command.visual_trajectory),
            "视觉置信度": command.visual_confidence,
            "input_dispatched": bool(receipt.data.get("input_dispatched", receipt.success)),
            "按下停顿毫秒": delays[0],
            "移动间隔范围毫秒": {"最小": min(delays[1:]), "最大": max(delays[1:])},
            "纵向偏移范围": {"最小": min(vertical), "最大": max(vertical)},
            "预按接近点数": VISUAL_DRAG_APPROACH_POINTS,
            "预按接近与悬停毫秒": VISUAL_DRAG_APPROACH_DURATION_MS,
        }
        for key in (
            "执行方式",
            "可视指针反馈",
            "动态视觉帧",
            "起点命中",
            "释放点命中",
            "拖后像素变化",
        ):
            if key in receipt.data:
                audit[key] = receipt.data[key]
        return audit

    @staticmethod
    def _required_ratio(arguments: dict[str, Any], key: str) -> float:
        value = arguments.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"工具参数 {key} 必须是数值")
        ratio = float(value)
        if not math.isfinite(ratio) or not 0 <= ratio <= 1:
            raise ValueError(f"工具参数 {key} 必须在 0 到 1 之间")
        return ratio

    @staticmethod
    def _build_plan_step(
        command: ActionCommand,
        candidate: CandidateTarget | None,
        input_key: str | None,
    ) -> PlanStep | None:
        if command.kind in {
            ActionKind.SCREENSHOT,
            ActionKind.WAIT,
            ActionKind.EVALUATE,
            ActionKind.DRAG,
            ActionKind.VISUAL_CLICK,
            ActionKind.INSPECT_VISUAL_REGION,
            ActionKind.VISUAL_DRAG,
        }:
            return None
        if command.kind is ActionKind.NAVIGATE:
            # 带查询参数的导航可能含一次性业务状态，不自动固化。
            if not command.url or urlsplit(command.url).query:
                return None
            return PlanStep(
                action_kind=command.kind,
                static_value=command.url,
                expected_kind=command.expected.kind if command.expected else None,
                expected_value=command.expected.value if command.expected else None,
                idempotent=True,
            )
        if command.kind is ActionKind.INPUT_TEXT and not input_key:
            # 字面量输入是一次性的(搜索词、备注)，不进跨任务快速路径。
            return None
        expected_value = command.expected.value if command.expected else None
        if command.expected and command.expected.kind == "target_exists":
            # 旧 target_id 含观察版本和临时节点 ID，不能写入跨任务快速路径。
            if candidate is None or command.expected.value != candidate.target_id:
                return None
            expected_value = CURRENT_TARGET_REFERENCE
        return PlanStep(
            action_kind=command.kind,
            target_role=candidate.role if candidate else None,
            target_name=candidate.name if candidate else None,
            input_key=input_key,
            static_value=None if input_key else command.value,
            expected_kind=command.expected.kind if command.expected else None,
            expected_value=expected_value,
            idempotent=command.idempotent,
        )

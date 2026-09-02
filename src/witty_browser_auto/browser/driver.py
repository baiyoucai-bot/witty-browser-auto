"""把原生 CDP 能力收敛为项目 AutomationDriver。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from witty_browser_auto.browser.annotation import (
    ANNOTATION_CLEANUP_SCRIPT,
    ANNOTATION_SCRIPT,
    AnnotationLabel,
    drawn_labels,
    overlay_payload,
)
from witty_browser_auto.browser.annotation import CONTAINER_ID as ANNOTATION_CONTAINER_ID
from witty_browser_auto.browser.diagnostics import DIAGNOSTIC_PAGE_SCRIPT, CdpPageDiagnostics
from witty_browser_auto.browser.dialogs import DIALOG_KINDS
from witty_browser_auto.browser.downloads import DownloadTracker
from witty_browser_auto.browser.drag_drop import drag_between_points
from witty_browser_auto.browser.drag_support import (
    is_native_range,
    set_native_range_from_drag,
    visual_pixels_changed,
    visual_point_diagnostic,
)
from witty_browser_auto.browser.element_inspect import read_element_state
from witty_browser_auto.browser.emulation import (
    EmulationState,
)
from witty_browser_auto.browser.emulation import (
    apply_state as apply_emulation_state,
)
from witty_browser_auto.browser.emulation import (
    clear_state as clear_emulation_state,
)
from witty_browser_auto.browser.emulation import (
    read_effective as read_effective_emulation,
)
from witty_browser_auto.browser.files import set_file_input_files
from witty_browser_auto.browser.form_fill import FormField
from witty_browser_auto.browser.form_fill import apply_field as apply_form_field
from witty_browser_auto.browser.frames import FrameHandle, FrameRegistry
from witty_browser_auto.browser.keyboard import dispatch_key_press
from witty_browser_auto.browser.locator import (
    focus_object,
    resolve_explicit_locator,
    resolve_locator_object,
)
from witty_browser_auto.browser.mouse import dispatch_click as dispatch_pointer_click
from witty_browser_auto.browser.mouse import dispatch_drag
from witty_browser_auto.browser.mouse import dispatch_hover as dispatch_pointer_hover
from witty_browser_auto.browser.navigation import expect_navigation_settled
from witty_browser_auto.browser.operation_recorder import (
    BrowserOperationSink,
    CdpUserOperationRecorder,
)
from witty_browser_auto.browser.page_content import PAGE_LINKS_SCRIPT, PAGE_MARKDOWN_SCRIPT
from witty_browser_auto.browser.page_export import export_pdf as export_page_pdf
from witty_browser_auto.browser.performance import (
    install_collector as install_performance_collector,
)
from witty_browser_auto.browser.performance import (
    read_counters as read_performance_counters,
)
from witty_browser_auto.browser.performance import (
    read_metrics as read_performance_metrics,
)
from witty_browser_auto.browser.ranking import rank_candidates, viewport_height_of
from witty_browser_auto.browser.scripts import (
    PAGE_STATE_SCRIPT,
    POINTER_TARGETS_SCRIPT,
    WAIT_FOR_ACTIONABLE_DOM_SCRIPT,
)
from witty_browser_auto.browser.session import CdpBrowser, CdpTargetSession
from witty_browser_auto.browser.storage import (
    read_cookies as read_page_cookies,
)
from witty_browser_auto.browser.storage import (
    read_web_storage as read_frame_web_storage,
)
from witty_browser_auto.browser.storage import (
    set_cookie as set_page_cookie,
)
from witty_browser_auto.browser.storage import (
    write_web_storage as write_frame_web_storage,
)
from witty_browser_auto.browser.storage_state import (
    export_state as export_session_state,
)
from witty_browser_auto.browser.storage_state import (
    import_state as import_session_state,
)
from witty_browser_auto.browser.verification import verify_condition
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.config import BrowserConfig, BrowserSessionMode
from witty_browser_auto.domain.errors import (
    ActionOutcomeUnknownError,
    CdpCommandError,
    CdpDisconnectedError,
    TargetNotFoundError,
)
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionKind,
    ActionReceipt,
    BoundingBox,
    CandidateTarget,
    DragRiskClass,
    DriverCapabilities,
    ExpectedCondition,
    LocatorRecipe,
    Observation,
    VerificationResult,
)
from witty_browser_auto.network.capture import CdpNetworkCapture
from witty_browser_auto.network.recorder import CdpNetworkRecorder
from witty_browser_auto.network.robots import MAX_ROBOTS_BYTES, ROBOTS_FETCH_SCRIPT
from witty_browser_auto.network.traffic import NetworkTrafficLog
from witty_browser_auto.security.redaction import redact_url

logger = logging.getLogger(__name__)


def _sanitize_visual_resources(value: Any) -> tuple[tuple[str, str, int, int], ...]:
    """只保留浏览器本地生成的图片哈希和尺寸，避免资源地址进入观察数据。"""

    if not isinstance(value, list):
        return ()
    resources: list[tuple[str, str, int, int]] = []
    for item in value[:50]:
        if not isinstance(item, list) or len(item) != 4:
            continue
        source_hash, pixel_hash, width, height = item
        if not isinstance(source_hash, str) or not isinstance(pixel_hash, str):
            continue
        if isinstance(width, bool) or not isinstance(width, int):
            continue
        if isinstance(height, bool) or not isinstance(height, int):
            continue
        resources.append(
            (
                source_hash[:16],
                pixel_hash[:16],
                max(0, min(width, 100_000)),
                max(0, min(height, 100_000)),
            )
        )
    return tuple(resources)


def _box_in_viewport(box: Any, origin: tuple[float, float]) -> Any:
    """帧内元素的包围盒是帧局部坐标，对外统一换算成主框架视口坐标。"""

    if not isinstance(box, dict) or origin == (0.0, 0.0):
        return box
    return {
        **box,
        "x": round(float(box.get("x", 0.0)) + origin[0], 2),
        "y": round(float(box.get("y", 0.0)) + origin[1], 2),
    }


def element_screenshot_clip(
    box: dict[str, Any],
    scroll: tuple[float, float],
    padding: float,
) -> dict[str, float]:
    """把元素的视口包围盒换算成 `Page.captureScreenshot` 需要的页面坐标裁剪区。

    clip 始终按页面坐标解释，与 `captureBeyondViewport` 无关；工具对外统一用视口坐标，
    因此这里必须补上页面滚动偏移，否则页面一旦滚动就会截到完全不相干的区域。
    """

    scroll_x, scroll_y = scroll
    return {
        "x": max(float(box["x"]) + scroll_x - padding, 0.0),
        "y": max(float(box["y"]) + scroll_y - padding, 0.0),
        "width": min(float(box["width"]) + padding * 2, _MAX_ELEMENT_SHOT_EDGE),
        "height": min(float(box["height"]) + padding * 2, _MAX_ELEMENT_SHOT_EDGE),
        "scale": 1,
    }


def _pointer_candidate_name(
    raw_name: object,
    raw_text: object,
    attributes: dict[str, str],
) -> str:
    """图标按钮优先使用稳定语义属性，避免模型只看到私有字体字符。"""

    name = CdpAutomationDriver._normalize_text(raw_name)[:200]
    text = CdpAutomationDriver._normalize_text(raw_text)[:200]
    if any(character.isalnum() for character in name):
        return name
    for key in ("aria-label", "title", "name", "data-testid", "id", "placeholder"):
        value = CdpAutomationDriver._normalize_text(attributes.get(key))[:200]
        if value:
            return value
    return name or text


# 一次观察最多登记多少个可寻址候选。这是"能用 target_id 指到谁"的上限，不是喂给模型的
# 数量——模型视图另有 24 个的预算(见 toolkit.serialization)。长列表页动辄两三百个链接，
# 200 会把页脚的"下一页"挤掉；400 在候选缓存与指纹计算上仍然很便宜。
MAX_OBSERVATION_CANDIDATES = 400


def _observation_fingerprint(
    url: str,
    title: str,
    candidates: list[CandidateTarget],
    visual_drag_risk: DragRiskClass,
    visual_resources: tuple[tuple[str, str, int, int], ...],
    text: str = "",
) -> str:
    """页面状态指纹：候选、可见文本、可见图片任一变化即视为页面已变化。

    可见文本必须参与：展开/收起、加减购物车数量、状态文案切换这类最常见的点击只
    改文字不改候选清单，若指纹对文本失明，这些成功的动作会被 `fingerprint_changed`
    判为失败并耗尽整个校验超时。文本取与页面摘要一致的前 3000 字符并折叠空白，
    与 `text_contains` 校验读到的是同一份内容。
    """

    source = {
        "url": url,
        "title": title,
        # 与候选展示顺序无关：滚动或聚焦引起的重排不该被当成页面变化。
        "targets": sorted(
            (candidate.role, candidate.name, candidate.drag_risk.value) for candidate in candidates
        ),
        "visual_drag_risk": visual_drag_risk.value,
        "visual_resources": visual_resources,
        "text": " ".join(text[:3000].split()),
    }
    return hashlib.sha256(
        json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


_ACTIONABLE_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "link",
    "listbox",
    "menuitem",
    "option",
    "radio",
    "searchbox",
    "slider",
    "spinbutton",
    "switch",
    "tab",
    "textbox",
}
_DOM_ACTIONABLE_TAGS = {"a", "button", "input", "option", "select", "summary", "textarea"}
_STABLE_ATTRIBUTE_NAMES = (
    "data-testid",
    "id",
    "name",
    "aria-label",
    "placeholder",
    "title",
    "href",
    "target",
    "type",
)
_NEW_PAGE_TIMEOUT_SECONDS = 3.0
_MAX_ELEMENT_SHOT_PADDING = 200.0
_MAX_ELEMENT_SHOT_EDGE = 8000.0
_BROWSER_ENVIRONMENT_SCRIPT = (
    "(()=>({webdriver:navigator.webdriver===true,userAgent:navigator.userAgent||'',"
    "language:navigator.language||'',platform:navigator.platform||'',"
    "timezone:Intl.DateTimeFormat().resolvedOptions().timeZone||'',"
    "visibilityState:document.visibilityState||''}))()"
)
_DOCUMENT_TEXT_SCRIPT = "function(){return this.body ? this.body.innerText : '';}"

_CLEAR_INPUT_SCRIPT = (
    "function(){if('value' in this){this.value='';"
    "this.dispatchEvent(new Event('input',{bubbles:true}));}}"
)
_INPUT_VALUE_MATCH_SCRIPT = (
    "function(expected){return 'value' in this&&String(this.value)===String(expected);}"
)
_SELECT_VALUE_SCRIPT = (
    "function(value){this.value=value;"
    "this.dispatchEvent(new Event('input',{bubbles:true}));"
    "this.dispatchEvent(new Event('change',{bubbles:true}));return this.value;}"
)
_HIT_TEST_SCRIPT = (
    "function(x,y){const hit=document.elementFromPoint(x,y);"
    "return !!hit&&(hit===this||this.contains(hit));}"
)
_POINTER_TARGET_STATE_SCRIPT = r"""
function() {
  const text = (this.innerText || this.textContent || '').replace(/\s+/g, ' ').trim();
  const disabled = this.hasAttribute('disabled') || this.getAttribute('aria-disabled') === 'true';
  return {text, disabled};
}
"""
_SECURITY_CHALLENGE_MARKERS = (
    "captcha",
    "human verification",
    "i am not a robot",
    "i'm not a robot",
    "robot verification",
    "security challenge",
    "verify you are human",
    "verify you're human",
    "图形验证码",
    "图片验证码",
    "人机验证",
    "安全验证",
    "拖动滑块完成验证",
    "滑动验证",
    "真人验证",
)
_AUTHENTICATION_ACTION_MARKERS = ("account login", "log in", "sign in", "登录")
_AUTHENTICATION_CREDENTIAL_MARKERS = ("password", "密码")
_SECURITY_URL_MARKERS = (
    "/auth",
    "/captcha",
    "/challenge",
    "/human-verification",
    "/login",
    "/robot-check",
    "/signin",
)
_OPERATION_RECORDER_CLEANUP_SECONDS = 0.25


def _takeover_page_matches(current_url: str, task_url: str) -> bool:
    """接管只复用同一站点同一路径，查询串和页面锚点不影响页面身份。"""

    try:
        current = urlsplit(current_url)
        expected = urlsplit(task_url)
        current_port = current.port
        expected_port = expected.port
    except ValueError:
        return False
    if current.scheme.casefold() not in {"http", "https", "file"}:
        return False
    current_scheme = current.scheme.casefold()
    expected_scheme = expected.scheme.casefold()
    default_ports = {"http": 80, "https": 443}
    current_origin = (
        current_scheme,
        (current.hostname or "").casefold(),
        current_port or default_ports.get(current_scheme),
    )
    expected_origin = (
        expected_scheme,
        (expected.hostname or "").casefold(),
        expected_port or default_ports.get(expected_scheme),
    )
    current_path = current.path.rstrip("/") or "/"
    expected_path = expected.path.rstrip("/") or "/"
    return current_origin == expected_origin and current_path == expected_path


class CdpAutomationDriver:
    def __init__(
        self,
        browser_config: BrowserConfig,
        artifact_root: Path,
        *,
        network_capture: CdpNetworkCapture | None = None,
        network_traffic: NetworkTrafficLog | None = None,
        operation_sink: BrowserOperationSink | None = None,
    ) -> None:
        self.browser_config = browser_config
        self.browser = CdpBrowser(browser_config)
        self.emulation_state: EmulationState | None = None
        self.artifact_root = artifact_root
        self.network_capture = network_capture
        self.network_traffic = network_traffic
        self.context_id: str | None = None
        self.session: CdpTargetSession | None = None
        self._owned_target_ids: set[str] = set()
        self._borrowed_target_ids: set[str] = set()
        self._candidate_cache: dict[str, CandidateTarget] = {}
        self._frames: FrameRegistry | None = None
        self._verification_frame_id: str | None = None
        self._last_observation_fingerprint: str | None = None
        self._last_known_url = ""
        self._page_recovered_since_observation = False
        self._action_lock = asyncio.Lock()
        self._session_recovery_lock = asyncio.Lock()
        self.network_recorder: CdpNetworkRecorder | None = None
        self.page_diagnostics: CdpPageDiagnostics | None = None
        self.download_tracker: DownloadTracker | None = None
        self.operation_recorder = CdpUserOperationRecorder(operation_sink)
        self.environment_diagnostics: dict[str, Any] = {}
        self._preserve_page_on_next_open = False
        self._takeover_page_selection_pending = False

    @property
    def last_known_url(self) -> str:
        """最近一次导航或观察记录的页面地址；重放需要它判断同源关系。"""

        return self._last_known_url

    @property
    def capabilities(self) -> DriverCapabilities:
        return DriverCapabilities(
            dom=True,
            accessibility=True,
            visual=True,
            network=True,
            files=True,
            storage=True,
            dialogs=True,
            emulation=True,
            forms=True,
            storage_state=True,
            element_drag=True,
            pdf_export=True,
            performance=True,
            windows=False,
            javascript=True,
        )

    @property
    def is_healthy(self) -> bool:
        session = self.session
        process = self.browser.managed_process
        return bool(
            session
            and self.browser.is_session_active(session)
            and (process is None or process.process.returncode is None)
        )

    @property
    def is_recoverable(self) -> bool:
        """浏览器连接仍在时，即使页面被用户关闭也可以原进程恢复。"""

        connection = self.browser.connection
        process = self.browser.managed_process
        return bool(
            connection
            and not connection.closed
            and (process is None or process.process.returncode is None)
        )

    def rebind_task_context(
        self,
        artifact_root: Path,
        network_capture: CdpNetworkCapture | None,
        operation_sink: BrowserOperationSink | None,
    ) -> None:
        """切换本轮产物接收器，并让下一次 open 沿用当前页面。"""

        if self.session and self.network_capture and self.network_capture is not network_capture:
            self.network_capture.discard_session(self.session.session_id)
        self.artifact_root = artifact_root
        self.network_capture = network_capture
        self.operation_recorder.sink = operation_sink
        if self.network_recorder:
            self.network_recorder.capture = network_capture
        self._preserve_page_on_next_open = True
        self._takeover_page_selection_pending = (
            self.browser_config.session_mode is BrowserSessionMode.TAKEOVER
        )

    async def start(self) -> None:
        await self.browser.start()
        takeover_requested = self.browser_config.session_mode is BrowserSessionMode.TAKEOVER
        if takeover_requested or self.browser.reattached:
            self.session = await self.browser.claim_existing_page()
            self._preserve_page_on_next_open = self.session is not None
            self._takeover_page_selection_pending = takeover_requested and self.session is not None
        if self.session is None:
            if takeover_requested:
                # 已连接用户 Chrome；没有普通活动页时仍在该浏览器中新建任务窗口。
                self.session = await self.browser.create_window(None)
            else:
                if not self.browser.config.reuse_profile:
                    self.context_id = await self.browser.create_context()
                self.session = await self.browser.create_page(self.context_id)
        elif takeover_requested:
            self._borrowed_target_ids.add(self.session.target_id)
        self.browser.remember_target(self.session.target_id)
        if self.session.target_id not in self._borrowed_target_ids:
            self._owned_target_ids.add(self.session.target_id)
        self.network_recorder = CdpNetworkRecorder(
            self.session,
            capture=self.network_capture,
            traffic=self.network_traffic,
        )
        await self.network_recorder.start(self.session.target_id)
        self.page_diagnostics = CdpPageDiagnostics(self.session)
        await self.page_diagnostics.start()
        await self._start_download_tracker()
        await self.operation_recorder.start(self.session)
        await self._start_frame_registry(self.session)
        try:
            self.environment_diagnostics = await self.browser_environment_snapshot()
        except CdpCommandError as exc:
            logger.warning(
                "读取浏览器运行环境失败，任务继续执行",
                extra={"cdp_method": exc.method, "cdp_error_code": exc.error_code},
            )
        logger.info(
            "浏览器运行环境已检查",
            extra={
                "headless": self.browser.config.headless,
                "profile_reused": self.browser.config.reuse_profile,
                "session_mode": self.browser.config.session_mode.value,
                "webdriver": self.environment_diagnostics.get("webdriver"),
                "language": self.environment_diagnostics.get("language"),
                "timezone": self.environment_diagnostics.get("timezone"),
                "visibility_state": self.environment_diagnostics.get("visibilityState"),
                "headless_user_agent": "HeadlessChrome"
                in str(self.environment_diagnostics.get("userAgent", "")),
            },
        )

    async def browser_environment_snapshot(self) -> dict[str, Any]:
        """读取有限的非敏感环境信号，用于解释站点兼容与挑战失败。"""

        await self._ensure_active_page()
        result = await self._require_session().call(
            "Runtime.evaluate",
            {"expression": _BROWSER_ENVIRONMENT_SCRIPT, "returnByValue": True},
        )
        value = result.get("result", {}).get("value", {})
        if not isinstance(value, dict):
            return {}
        allowed_keys = (
            "webdriver",
            "userAgent",
            "language",
            "platform",
            "timezone",
            "visibilityState",
        )
        return {
            key: item for key in allowed_keys if isinstance((item := value.get(key)), (bool, str))
        }

    async def request_page_attention(self) -> None:
        """激活当前标签页，并在窗口被最小化时先恢复为普通窗口。"""

        await self._ensure_active_page()
        session = self._require_session()
        try:
            window = await session.connection.call(
                "Browser.getWindowForTarget",
                {"targetId": session.target_id},
            )
            window_id = window.get("windowId")
            bounds = window.get("bounds")
            if (
                isinstance(window_id, int)
                and isinstance(bounds, dict)
                and bounds.get("windowState") == "minimized"
            ):
                await session.connection.call(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "normal"}},
                )
        except CdpCommandError as exc:
            logger.warning(
                "读取或恢复浏览器窗口状态失败，继续激活当前标签页",
                extra={"cdp_method": exc.method, "cdp_error_code": exc.error_code},
            )
        await session.call("Page.bringToFront")

    async def list_tabs(self) -> list[dict[str, Any]]:
        """列出当前浏览器页面标签；URL 统一去除查询参数后返回。"""

        lister = getattr(self.browser, "list_page_targets", None)
        if lister is None:
            raise TargetNotFoundError("当前浏览器不支持标签页列举")
        targets = await lister()
        current_target_id = self.session.target_id if self.session else None
        return [
            {
                "target_id": str(target["target_id"]),
                "url": redact_url(str(target.get("url", ""))),
                "title": str(target.get("title", ""))[:200],
                "is_current": target["target_id"] == current_target_id,
                "owned_by_task": target["target_id"] in self._owned_target_ids,
            }
            for target in targets
        ]

    async def open_tab(self, url: str) -> dict[str, Any]:
        """新建任务自有标签页并切换过去；地址授权由工具层按任务范围校验。"""

        async with self._action_lock:
            creator = getattr(self.browser, "create_page", None)
            if creator is None:
                raise TargetNotFoundError("当前浏览器不支持新建标签页")
            # 有模拟在生效时必须先建空白页再导航：直接带 URL 建页会在覆盖生效之前
            # 就把请求发出去，按 UA 分流的站点会返回桌面版。
            deferred_navigation = self.emulation_state is not None
            session = await creator(self.context_id, "about:blank" if deferred_navigation else url)
            # create_page 返回时页面可能仍在加载；沿用点击开新页的等待与接管路径。
            await self._adopt_page_session(session)
            if deferred_navigation:
                await session.call("Page.navigate", {"url": url})
            self.browser.remember_target(session.target_id)
            self._last_observation_fingerprint = None
            return {
                "target_id": session.target_id,
                "opened": True,
                "url": redact_url(url),
            }

    async def switch_tab(self, target_id: str) -> dict[str, Any]:
        """把任务操作面切换到指定标签页；借用的用户页面不会被任务关闭。"""

        async with self._action_lock:
            session = self.session
            if session is not None and session.target_id == target_id:
                return {"target_id": target_id, "switched": False, "already_current": True}
            attacher = getattr(self.browser, "attach_to_page", None)
            if attacher is None:
                raise TargetNotFoundError("当前浏览器不支持标签页切换")
            new_session = await attacher(target_id)
            borrowed = target_id not in self._owned_target_ids
            await self._adopt_page_session(new_session, borrowed=borrowed)
            self.browser.remember_target(target_id)
            self._last_observation_fingerprint = None
            return {"target_id": target_id, "switched": True, "borrowed": borrowed}

    async def close_tab(self, target_id: str) -> dict[str, Any]:
        """只关闭任务自己创建的标签页；关闭当前页后自动切换到其余任务页面。"""

        if target_id not in self._owned_target_ids:
            raise TargetNotFoundError("只能关闭任务自己创建的标签页，用户原有页面必须保留")
        async with self._action_lock:
            was_current = bool(self.session and self.session.target_id == target_id)
            await self.browser.close_target(target_id)
            self._owned_target_ids.discard(target_id)
            result: dict[str, Any] = {
                "target_id": target_id,
                "closed": True,
                "was_current": was_current,
            }
            if was_current:
                fallback_target_id = await self._switch_to_remaining_page()
                result["switched_to"] = fallback_target_id
            return result

    async def _switch_to_remaining_page(self) -> str | None:
        """当前任务页被关闭后，优先回到其余任务页，再考虑借用页面。"""

        lister = getattr(self.browser, "list_page_targets", None)
        attacher = getattr(self.browser, "attach_to_page", None)
        if lister is None or attacher is None:
            return None
        try:
            targets = await lister()
        except CdpCommandError:
            return None
        fallback = next(
            (target for target in targets if target["target_id"] in self._owned_target_ids),
            None,
        ) or next(
            (target for target in targets if target["target_id"] in self._borrowed_target_ids),
            None,
        )
        if fallback is None:
            return None
        target_id = str(fallback["target_id"])
        new_session = await attacher(target_id)
        await self._adopt_page_session(
            new_session,
            borrowed=target_id in self._borrowed_target_ids,
        )
        return target_id

    async def open(self, url: str) -> str:
        if not self.session:
            await self.start()
        else:
            await self._ensure_active_page(fallback_url=url)
        if self._preserve_page_on_next_open:
            self._preserve_page_on_next_open = False
            if self._takeover_page_selection_pending:
                self._takeover_page_selection_pending = False
                return await self._open_matching_takeover_page(url)
            return self._require_session().target_id
        await self._navigate(url, timeout_seconds=30)
        return self._require_session().target_id

    async def _open_matching_takeover_page(self, url: str) -> str:
        current_session = self._require_session()
        current_url = await self._current_page_url(current_session)
        if _takeover_page_matches(current_url, url):
            logger.info(
                "当前浏览器页面符合任务入口，继续使用原页面",
                extra={"target_id": current_session.target_id, "current_url": current_url},
            )
            return current_session.target_id

        new_session = await self.browser.create_window(None, url)
        previous_target_id = current_session.target_id
        await self._adopt_page_session(new_session)
        self.browser.remember_target(new_session.target_id)
        self._last_known_url = url
        if previous_target_id in self._owned_target_ids:
            try:
                await self.browser.close_target(previous_target_id)
            except Exception:
                logger.warning(
                    "关闭已替换的任务窗口失败",
                    extra={"target_id": previous_target_id},
                )
            else:
                self._owned_target_ids.discard(previous_target_id)
        logger.info(
            "当前浏览器页面不符合任务入口，已在同一浏览器新建任务窗口",
            extra={
                "previous_target_id": previous_target_id,
                "target_id": new_session.target_id,
                "current_url": current_url,
                "task_url": url,
            },
        )
        return new_session.target_id

    @staticmethod
    async def _current_page_url(session: CdpTargetSession) -> str:
        try:
            result = await session.call(
                "Runtime.evaluate",
                {"expression": "window.location.href", "returnByValue": True},
            )
        except CdpCommandError:
            return ""
        value = result.get("result", {}).get("value")
        return value if isinstance(value, str) else ""

    async def observe(self, *, force: bool = False) -> Observation:
        await self._ensure_active_page()
        session = self._require_session()
        # load 事件不代表 SPA 已完成挂载；只在可交互 DOM 仍为空时事件化等待，避免盲目休眠。
        await self._optional_observation_call(
            "Runtime.evaluate",
            {
                "expression": WAIT_FOR_ACTIONABLE_DOM_SCRIPT,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        page_state, ax_result, dom_result, pointer_result = await asyncio.gather(
            session.call(
                "Runtime.evaluate",
                {
                    "expression": PAGE_STATE_SCRIPT,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            ),
            self._optional_observation_call("Accessibility.getFullAXTree"),
            self._optional_observation_call(
                "DOM.getDocument",
                {"depth": -1, "pierce": True},
            ),
            self._optional_observation_call(
                "Runtime.evaluate",
                {"expression": POINTER_TARGETS_SCRIPT, "returnByValue": True},
            ),
        )
        value = page_state.get("result", {}).get("value", {})
        if not isinstance(value, dict):
            value = {}
        url = str(value.get("url", ""))
        if url and url != "about:blank":
            self._last_known_url = url
        title = str(value.get("title", ""))
        text = str(value.get("text", ""))
        visual_resources = _sanitize_visual_resources(value.get("visualResources"))

        ax_nodes = ax_result.get("nodes", [])
        page_drag_risk, page_drag_risk_reasons = self._classify_page_drag_risk(
            url,
            title,
            text,
        )
        viewport = value.get("viewport")
        css_viewport = (
            {"width": viewport.get("width"), "height": viewport.get("height")}
            if isinstance(viewport, dict)
            else {}
        )
        candidates = await self._build_candidates(
            ax_nodes if isinstance(ax_nodes, list) else [],
            dom_result.get("root") if isinstance(dom_result.get("root"), dict) else None,
            self._pointer_target_values(pointer_result),
            page_drag_risk=page_drag_risk,
            page_drag_risk_reasons=page_drag_risk_reasons,
            viewport_height=viewport_height_of({"CSS视口": css_viewport}),
        )
        fingerprint = _observation_fingerprint(
            url,
            title,
            candidates,
            page_drag_risk,
            visual_resources,
            text=text,
        )
        summary = f"页面标题：{title}\n页面地址：{url}\n页面文本摘要：{text[:3000]}"
        self._candidate_cache = {candidate.target_id: candidate for candidate in candidates}
        self._last_observation_fingerprint = fingerprint
        recovered = self._page_recovered_since_observation
        self._page_recovered_since_observation = False
        return Observation(
            surface_id=session.target_id,
            url=url,
            title=title,
            version=session.observation_version,
            fingerprint=fingerprint,
            summary=summary,
            candidates=tuple(candidates),
            visual_drag_risk=page_drag_risk,
            visual_drag_risk_reasons=page_drag_risk_reasons,
            metadata={
                "候选数量": len(candidates),
                "视觉拖拽风险": page_drag_risk.value,
                "浏览器运行环境": dict(self.environment_diagnostics),
                "CSS视口": css_viewport,
                "页面会话已恢复": recovered,
            },
        )

    async def _optional_observation_call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._require_session().call(method, params)
        except CdpCommandError as exc:
            logger.warning(
                "页面语义观察域不可用，继续使用其余定位来源",
                extra={"cdp_method": method, "cdp_error_code": exc.error_code},
            )
            return {}

    async def execute(self, command: ActionCommand) -> ActionReceipt:
        started = time.perf_counter()
        # 后置校验紧跟本次动作，文本条件要读动作实际作用的那个帧。
        self._verification_frame_id = command.locator.frame_id if command.locator else None
        try:
            # 单条 CDP 调用有超时还不够：拖拽会串行发送几十个事件。整次动作必须共享
            # 一个截止时间，避免每个事件和清理步骤都重新获得完整预算。
            async with asyncio.timeout(command.timeout_seconds):
                async with self._action_lock:
                    if await self._ensure_active_page(
                        fallback_url=(command.url or "")
                        if command.kind is ActionKind.NAVIGATE
                        else "",
                    ):
                        return ActionReceipt(
                            action_id=command.action_id,
                            success=False,
                            outcome_known=True,
                            message="检测到页面已被关闭，已恢复上次页面；重新观察后再执行当前动作",
                            duration_ms=(time.perf_counter() - started) * 1000,
                            data={"browser_page_recovered": True, "input_dispatched": False},
                        )
                    await self.operation_recorder.set_agent_action(True)
                    try:
                        data = await self._execute_locked(command)
                    finally:
                        try:
                            async with asyncio.timeout(_OPERATION_RECORDER_CLEANUP_SECONDS):
                                await self.operation_recorder.set_agent_action(False)
                        except (CdpCommandError, CdpDisconnectedError, TimeoutError):
                            logger.warning(
                                "浏览器操作记录状态清理超时，主动作继续收敛",
                                extra={
                                    "action_id": command.action_id,
                                    "action_kind": command.kind.value,
                                },
                            )
            return ActionReceipt(
                action_id=command.action_id,
                success=True,
                outcome_known=True,
                message="浏览器动作已执行，仍需校验业务结果",
                duration_ms=(time.perf_counter() - started) * 1000,
                data=data,
            )
        except CdpDisconnectedError as exc:
            logger.error(
                "执行动作时 CDP 连接中断，动作结果未知",
                extra={"action_id": command.action_id, "action_kind": command.kind.value},
            )
            return ActionReceipt(
                action_id=command.action_id,
                success=False,
                outcome_known=False,
                message=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except ActionOutcomeUnknownError as exc:
            logger.error(
                "拖拽动作在指针按下后中断，页面结果未知",
                extra={"action_id": command.action_id, "action_kind": command.kind.value},
            )
            return ActionReceipt(
                action_id=command.action_id,
                success=False,
                outcome_known=False,
                message=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except CdpCommandError as exc:
            outcome_known = exc.error_code is not None
            logger.warning(
                "浏览器动作的 CDP 命令执行失败",
                extra={
                    "action_id": command.action_id,
                    "action_kind": command.kind.value,
                    "outcome_known": outcome_known,
                    "cdp_method": exc.method,
                },
            )
            return ActionReceipt(
                action_id=command.action_id,
                success=False,
                outcome_known=outcome_known,
                message=f"{exc.method}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except TimeoutError:
            logger.warning(
                "浏览器动作超过总时间预算",
                extra={
                    "action_id": command.action_id,
                    "action_kind": command.kind.value,
                    "timeout_seconds": command.timeout_seconds,
                },
            )
            return ActionReceipt(
                action_id=command.action_id,
                success=False,
                outcome_known=False,
                message=f"浏览器动作超过 {command.timeout_seconds:g} 秒总时间预算",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            logger.warning(
                "浏览器动作执行失败",
                extra={"action_id": command.action_id, "action_kind": command.kind.value},
            )
            return ActionReceipt(
                action_id=command.action_id,
                success=False,
                outcome_known=True,
                message=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000,
                data=({"input_dispatched": False} if isinstance(exc, TargetNotFoundError) else {}),
            )

    async def verify(self, condition: ExpectedCondition) -> VerificationResult:
        return await verify_condition(self, condition)

    async def _verification_document_text(self) -> str:
        """读取后置校验应当观察的文档文本；帧内动作看它自己的帧。"""

        frame_id = self._verification_frame_id
        if frame_id is None:
            result = await self._require_session().call(
                "Runtime.evaluate",
                {"expression": "document.body?.innerText || ''", "returnByValue": True},
            )
        else:
            frame = await (await self._require_frames()).resolve(frame_id)
            result = await frame.call_on_document(_DOCUMENT_TEXT_SCRIPT)
        value = result.get("result", {}).get("value")
        return value if isinstance(value, str) else ""

    async def capture_evidence(self, label: str) -> Path:
        image_bytes = await self._capture_screenshot_bytes()
        return self._write_evidence_bytes(label, image_bytes)

    def _write_evidence_bytes(self, label: str, image_bytes: bytes) -> Path:
        """以私有权限独占写入截图，避免并发覆盖或扩大本机读取范围。"""
        safe_label = "".join(
            character for character in label if character.isalnum() or character in "-_"
        )
        path = self.artifact_root / f"{safe_label or 'evidence'}-{time.time_ns()}.png"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(image_bytes)
        logger.info("浏览器证据截图已保存", extra={"path": str(path)})
        return path

    async def _capture_screenshot_bytes(self) -> bytes:
        result = await self._require_session().call(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
        )
        data = result.get("data")
        if not isinstance(data, str):
            raise RuntimeError("浏览器未返回截图数据")
        return base64.b64decode(data)

    async def close(self) -> None:
        await self.operation_recorder.close()
        if self.download_tracker is not None:
            self.download_tracker.close()
            self.download_tracker = None
        if self.page_diagnostics:
            await self.page_diagnostics.close()
            self.page_diagnostics = None
        if self.network_recorder:
            await self.network_recorder.close()
            self.network_recorder = None
        if self._frames is not None:
            self._frames.close()
            self._frames = None
        if self.session and self.session.target_id not in self._borrowed_target_ids:
            self._owned_target_ids.add(self.session.target_id)
        for target_id in tuple(self._owned_target_ids):
            try:
                await self.browser.close_target(target_id)
            except Exception:
                logger.warning("关闭浏览器 Target 失败", extra={"target_id": target_id})
        self._owned_target_ids.clear()
        self._borrowed_target_ids.clear()
        self.session = None
        if self.context_id:
            try:
                await self.browser.dispose_context(self.context_id)
            except Exception:
                logger.warning("销毁 BrowserContext 失败")
            self.context_id = None
        await self.browser.close()

    async def network_snapshot(self) -> tuple[dict[str, Any], ...]:
        if not self.network_recorder:
            return ()
        return await self.network_recorder.snapshot()

    async def diagnostic_snapshot(
        self,
        *,
        max_console: int = 20,
        max_network: int = 30,
    ) -> dict[str, Any]:
        if self.page_diagnostics is None:
            return {"signals": ["diagnostics_not_started"]}
        page_result, network_records = await asyncio.gather(
            self._optional_observation_call(
                "Runtime.evaluate",
                {"expression": DIAGNOSTIC_PAGE_SCRIPT, "returnByValue": True},
            ),
            self.network_snapshot(),
        )
        page_state = page_result.get("result", {}).get("value", {})
        return self.page_diagnostics.snapshot(
            page_state=page_state if isinstance(page_state, dict) else {},
            network_records=network_records,
            environment=self.environment_diagnostics,
            max_console=max_console,
            max_network=max_network,
        )

    async def inspect_element(
        self,
        *,
        target_id: str | None = None,
        locator: LocatorRecipe | None = None,
        max_text_length: int = 2000,
        include_html: bool = False,
    ) -> dict[str, Any]:
        """只读读取单个元素的白名单状态；不滚动页面也不改变焦点。"""

        object_id, source, session, origin = await self._resolve_readable_object(target_id, locator)
        state = await read_element_state(
            session,
            object_id,
            max_text_length=max_text_length,
            include_html=include_html,
        )
        return {**state, "box": _box_in_viewport(state.get("box"), origin), "resolved_by": source}

    async def capture_element_screenshot(
        self,
        *,
        target_id: str | None = None,
        locator: LocatorRecipe | None = None,
        label: str = "element",
        padding: float = 0.0,
    ) -> dict[str, Any]:
        """只截取单个元素所在矩形；元素在视口外也不滚动页面。"""

        if not 0.0 <= padding <= _MAX_ELEMENT_SHOT_PADDING:
            raise ValueError(f"截图外扩必须在 0 到 {_MAX_ELEMENT_SHOT_PADDING} 像素之间")
        object_id, source, session, origin = await self._resolve_readable_object(target_id, locator)
        state = await read_element_state(session, object_id, max_text_length=0)
        box = _box_in_viewport(state.get("box"), origin)
        if not isinstance(box, dict) or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
            raise TargetNotFoundError("目标元素没有可见矩形，无法单独截图")
        page_session = self._require_session()
        scroll = await self._page_scroll_offset(page_session)
        clip = element_screenshot_clip(box, scroll, padding)
        result = await page_session.call(
            "Page.captureScreenshot",
            # 视口外元素只有开启 captureBeyondViewport 才能直接截到，无需先滚动。
            {"format": "png", "clip": clip, "captureBeyondViewport": True},
        )
        data = result.get("data")
        if not isinstance(data, str):
            raise RuntimeError("浏览器未返回元素截图数据")
        path = self._write_evidence_bytes(label, base64.b64decode(data))
        return {
            "screenshot_path": str(path),
            "box": box,
            "clip": {key: round(value, 2) for key, value in clip.items() if key != "scale"},
            "resolved_by": source,
        }

    async def fetch_robots_txt(self, origin: str) -> dict[str, Any]:
        """在页面上下文取该站点的 robots.txt。

        地址由本方法按 origin 自行拼成，调用方无法把它指向任意 URL；请求走浏览器网络栈
        且不带凭据，robots.txt 是公开策略文件。
        """

        parts = urlsplit(origin)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("robots.txt 只能针对 http/https 站点查询")
        payload = json.dumps(
            {
                "url": f"{parts.scheme}://{parts.netloc}/robots.txt",
                "maxBytes": MAX_ROBOTS_BYTES,
            },
            ensure_ascii=False,
        )
        result = await self._require_session().call(
            "Runtime.evaluate",
            {
                "expression": f"({ROBOTS_FETCH_SCRIPT.strip()})({payload})",
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        exception = result.get("exceptionDetails")
        if isinstance(exception, dict):
            return {"ok": False, "error": str(exception.get("text", "robots.txt 读取失败"))}
        value = result.get("result", {}).get("value")
        if not isinstance(value, str):
            return {"ok": False, "error": "浏览器未返回 robots.txt 结果"}
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"ok": False, "error": "结果格式不正确"}

    async def read_page_markdown(self, options: dict[str, Any]) -> dict[str, Any]:
        """把当前页面的主内容转成 Markdown；只读渲染结果，不发起独立请求。"""

        payload = json.dumps(options, ensure_ascii=False)
        result = await self._require_session().call(
            "Runtime.evaluate",
            {
                "expression": f"({PAGE_MARKDOWN_SCRIPT.strip()})({payload})",
                "returnByValue": True,
            },
        )
        value = result.get("result", {}).get("value")
        if not isinstance(value, str):
            raise RuntimeError("浏览器未返回页面 Markdown 结果")
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise RuntimeError("页面 Markdown 结果格式不正确")
        return parsed

    async def read_page_links(self, *, include_images: bool, scan_limit: int) -> dict[str, Any]:
        """列出页面链接与可选图片；地址统一换算成绝对地址。"""

        payload = json.dumps(
            {"includeImages": include_images, "scanLimit": scan_limit},
            ensure_ascii=False,
        )
        result = await self._require_session().call(
            "Runtime.evaluate",
            {
                "expression": f"({PAGE_LINKS_SCRIPT.strip()})({payload})",
                "returnByValue": True,
            },
        )
        value = result.get("result", {}).get("value")
        if not isinstance(value, str):
            raise RuntimeError("浏览器未返回页面链接结果")
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise RuntimeError("页面链接结果格式不正确")
        return parsed

    async def capture_annotated_screenshot(
        self,
        labels: Sequence[AnnotationLabel],
        *,
        label: str = "annotated",
    ) -> dict[str, Any]:
        """在当前视口截图上叠加编号标注；覆盖层用后必除，不滚动页面。"""

        session = self._require_session()
        payload = json.dumps(overlay_payload(labels), ensure_ascii=False)
        try:
            result = await session.call(
                "Runtime.evaluate",
                {
                    "expression": f"({ANNOTATION_SCRIPT.strip()})({payload})",
                    "returnByValue": True,
                },
            )
            value = result.get("result", {}).get("value")
            drawn = drawn_labels(json.loads(value) if isinstance(value, str) else None)
            image_bytes = await self._capture_screenshot_bytes()
        finally:
            # 覆盖层留在页面上会污染后续截图与用户视野，任何路径都必须清掉。
            try:
                await session.call(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            f"({ANNOTATION_CLEANUP_SCRIPT.strip()})"
                            f"({json.dumps(ANNOTATION_CONTAINER_ID)})"
                        ),
                        "returnByValue": True,
                    },
                )
            except Exception:
                logger.warning("标注覆盖层清理失败，页面可能残留标注", exc_info=True)
        return {
            "screenshot_path": str(self._write_evidence_bytes(label, image_bytes)),
            "drawn": list(drawn),
        }

    @staticmethod
    async def _page_scroll_offset(session: CdpTargetSession) -> tuple[float, float]:
        result = await session.call(
            "Runtime.evaluate",
            {"expression": "[window.scrollX, window.scrollY]", "returnByValue": True},
        )
        value = result.get("result", {}).get("value")
        if isinstance(value, list) and len(value) == 2:
            return float(value[0]), float(value[1])
        return 0.0, 0.0

    async def _resolve_readable_object(
        self,
        target_id: str | None,
        locator: LocatorRecipe | None,
    ) -> tuple[str, str, CdpTargetSession, tuple[float, float]]:
        """解析只读目标；读取允许禁用、被遮挡和视口外的元素。

        返回的帧原点用于把帧内包围盒换算回主框架视口，使读取结果与点击坐标同一坐标系。
        """

        if (target_id is None) == (locator is None):
            raise TargetNotFoundError("元素读取必须且只能提供 target_id 或显式定位器")
        if locator is not None:
            frame = await self._resolve_frame(locator)
            object_id = await resolve_locator_object(frame, locator)
            return object_id, "locator", frame.session, (frame.offset_x, frame.offset_y)
        session = self._require_session()
        candidate = self._candidate_cache.get(target_id or "")
        if candidate is None:
            raise TargetNotFoundError("目标区域不存在或页面观察已经过期")
        expected_prefix = f"{session.target_id}:{session.observation_version}:"
        if not (target_id or "").startswith(expected_prefix):
            raise TargetNotFoundError("页面状态已变化，必须重新观察后再读取目标区域")
        backend_node_id = candidate.recipe.backend_node_id
        if backend_node_id is None:
            raise TargetNotFoundError("目标定位配方缺少浏览器节点 ID")
        resolved = await session.call("DOM.resolveNode", {"backendNodeId": backend_node_id})
        object_id = resolved.get("object", {}).get("objectId")
        if not isinstance(object_id, str):
            raise TargetNotFoundError("无法解析目标区域")
        return object_id, "observation_candidate", session, (0.0, 0.0)

    async def _press_key(self, command: ActionCommand) -> dict[str, Any]:
        """把已编译的按键规格派发到当前页面，必要时先聚焦目标元素。"""

        session = self._require_session()
        try:
            resolved = json.loads(command.value or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("按键规格无效") from exc
        if not isinstance(resolved, dict):
            raise RuntimeError("按键规格必须是对象")
        focus_target = ""
        if command.target_id or command.locator is not None:
            object_id, focus_target, element_session, _origin = await self._resolve_readable_object(
                command.target_id, command.locator
            )
            await focus_object(element_session, object_id)
        repeat = int(resolved.pop("repeat", 1))
        audit = await dispatch_key_press(session, resolved, repeat=repeat)
        return {**audit, "focused": focus_target or "current_focus", "input_dispatched": True}

    async def _navigate_history(self, command: ActionCommand) -> dict[str, Any]:
        """执行后退、前进或重新加载，并等待页面重新就绪。"""

        session = self._require_session()
        action = command.value or ""
        async with expect_navigation_settled(
            session, timeout_seconds=command.timeout_seconds
        ) as settle:
            if action == "reload":
                await session.call("Page.reload", {"ignoreCache": False})
            else:
                await self._navigate_to_history_entry(session, action)
            settled_by = await settle()
        self._last_observation_fingerprint = None
        return {"history_action": action, "settled_by": settled_by}

    @staticmethod
    async def _navigate_to_history_entry(session: CdpTargetSession, action: str) -> None:
        history = await session.call("Page.getNavigationHistory")
        entries = history.get("entries")
        current_index = history.get("currentIndex")
        if not isinstance(entries, list) or not isinstance(current_index, int):
            raise RuntimeError("浏览器没有返回可用的页面历史")
        target_index = current_index - 1 if action == "back" else current_index + 1
        if not 0 <= target_index < len(entries):
            raise RuntimeError(
                "当前页面没有可后退的历史记录"
                if action == "back"
                else "当前页面没有可前进的历史记录"
            )
        entry_id = entries[target_index].get("id")
        if not isinstance(entry_id, int):
            raise RuntimeError("页面历史记录缺少可用条目 ID")
        await session.call("Page.navigateToHistoryEntry", {"entryId": entry_id})

    async def _execute_locked(self, command: ActionCommand) -> dict[str, Any]:
        if command.kind is ActionKind.NAVIGATE:
            await self._navigate(command.url or "", command.timeout_seconds)
            return {"url": command.url}
        if command.kind is ActionKind.WAIT:
            await asyncio.sleep(float(command.value or "1"))
            return {}
        if command.kind is ActionKind.SCROLL:
            amount = float(command.value or "600")
            await self._require_session().call(
                "Runtime.evaluate",
                {"expression": f"window.scrollBy(0, {amount})", "returnByValue": True},
            )
            return {"amount": amount}
        if command.kind is ActionKind.EVALUATE:
            result = await self._require_session().call(
                "Runtime.evaluate",
                {
                    "expression": command.script,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            return {"value": result.get("result", {}).get("value")}
        if command.kind is ActionKind.PRESS_KEY:
            return await self._press_key(command)
        if command.kind is ActionKind.NAVIGATE_HISTORY:
            return await self._navigate_history(command)
        if command.kind is ActionKind.SCREENSHOT:
            path = await self.capture_evidence(command.action_id)
            return {"path": str(path)}
        if command.kind is ActionKind.INSPECT_VISUAL_REGION:
            path = await self._capture_visual_region(self._require_session(), command)
            return {
                "path": str(path),
                "裁剪视口比例": command.visual_clip,
                "放大倍数": 2,
            }
        if command.kind is ActionKind.VISUAL_CLICK:
            await self._click_viewport(self._require_session(), command)
            return {
                "点击视口比例": {
                    "x": command.visual_x_ratio,
                    "y": command.visual_y_ratio,
                },
                "视觉置信度": command.visual_confidence,
                "input_dispatched": True,
            }
        if command.kind is ActionKind.VISUAL_DRAG:
            drag_diagnostics = await self._drag_viewport(self._require_session(), command)
            visual_endpoint = command.visual_trajectory[-1]
            return {
                "终点视口比例": {
                    "x": visual_endpoint.x_ratio,
                    "y": visual_endpoint.y_ratio,
                },
                "轨迹点数": len(command.visual_trajectory),
                "视觉置信度": command.visual_confidence,
                "拖拽风险": command.drag_risk.value if command.drag_risk else "unknown",
                "风险依据": list(command.drag_risk_reasons),
                "input_dispatched": True,
                **drag_diagnostics,
            }

        page_session = self._require_session()
        # 输入事件始终走页面会话并使用视口坐标；元素级协议调用必须留在元素所属帧的会话上。
        if command.locator is not None:
            frame = await self._resolve_frame(command.locator)
            candidate, box, object_id = await resolve_explicit_locator(frame, command.locator)
            session = frame.session
        else:
            candidate, box, object_id = await self._resolve_target(command.target_id or "")
            session = page_session
        if command.kind is ActionKind.HOVER:
            await dispatch_pointer_hover(page_session, *box.center)
            return {"target": candidate.name, "pointer": "hover"}
        if command.kind is ActionKind.CLICK:
            return await self._click_target(page_session, candidate, box, command)
        if command.kind is ActionKind.DRAG:
            drag_result = await self._drag_target(
                session, page_session, candidate, box, object_id, command
            )
            target_endpoint = command.trajectory[-1]
            return {
                "target": candidate.name,
                "终点偏移": {"x": target_endpoint.dx, "y": target_endpoint.dy},
                "轨迹点数": len(command.trajectory),
                "拖拽风险": command.drag_risk.value if command.drag_risk else "unknown",
                "风险依据": list(command.drag_risk_reasons),
                "input_dispatched": True,
                **drag_result,
            }
        if command.kind is ActionKind.INPUT_TEXT:
            if command.locator is not None:
                await focus_object(session, object_id)
            else:
                await session.call("DOM.focus", {"backendNodeId": candidate.recipe.backend_node_id})
            await session.call(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": _CLEAR_INPUT_SCRIPT,
                    "returnByValue": True,
                },
            )
            await page_session.call("Input.insertText", {"text": command.value or ""})
            matched = await session.call(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": _INPUT_VALUE_MATCH_SCRIPT,
                    "arguments": [{"value": command.value or ""}],
                    "returnByValue": True,
                },
            )
            if matched.get("result", {}).get("value") is not True:
                raise RuntimeError("文本已经发送，但输入框回读校验失败")
            return {
                "target": candidate.name,
                "输入长度": len(command.value or ""),
                "输入回读一致": True,
            }
        if command.kind is ActionKind.SELECT:
            await session.call(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": _SELECT_VALUE_SCRIPT,
                    "arguments": [{"value": command.value or ""}],
                    "returnByValue": True,
                },
            )
            return {"target": candidate.name}
        if command.kind is ActionKind.UPLOAD_FILES:
            files = await set_file_input_files(
                session,
                object_id,
                [Path(path) for path in command.file_paths],
            )
            return {
                "target": candidate.name,
                "files": files,
                "file_count": len(files),
            }
        raise ValueError(f"暂不支持的浏览器动作：{command.kind.value}")

    async def _click_target(
        self,
        session: CdpTargetSession,
        candidate: CandidateTarget,
        box: BoundingBox,
        command: ActionCommand,
    ) -> dict[str, Any]:
        pointer = {"button": command.pointer_button, "click_count": command.click_count}
        # 右键与中键不会跟随链接，只有普通左键单击才可能开出新标签页。
        opens_new_page = (
            command.pointer_button == "left"
            and command.click_count == 1
            and self._opens_new_page(candidate)
        )
        if not opens_new_page:
            await self._dispatch_click(session, box, command)
            return {"target": candidate.name, **pointer}

        # 必须在点击前注册浏览器级事件，否则快速创建的新标签页可能先于等待器出现。
        async with session.connection.expect_event(
            "Target.attachedToTarget",
            predicate=lambda event: self._is_page_opened_by(event, session.target_id),
        ) as page_opened:
            await self._dispatch_click(session, box, command)
            try:
                event = await asyncio.wait_for(
                    page_opened,
                    timeout=min(command.timeout_seconds, _NEW_PAGE_TIMEOUT_SECONDS),
                )
            except TimeoutError:
                logger.warning(
                    "链接声明打开新标签页，但限定时间内未观察到新页面",
                    extra={"target": candidate.name, "opener_target_id": session.target_id},
                )
                return {"target": candidate.name, **pointer, "new_page": False}

        target_info = event.params.get("targetInfo")
        target_id = target_info.get("targetId") if isinstance(target_info, dict) else None
        if not isinstance(target_id, str):
            raise RuntimeError("新标签页事件缺少 Target ID")
        new_session = await self.browser.wait_for_target_session(
            target_id,
            timeout_seconds=_NEW_PAGE_TIMEOUT_SECONDS,
        )
        await self._adopt_page_session(new_session)
        return {"target": candidate.name, **pointer, "new_page": True, "target_id": target_id}

    async def _adopt_page_session(
        self,
        session: CdpTargetSession,
        *,
        borrowed: bool = False,
    ) -> None:
        await session.call("Page.bringToFront")
        if self.page_diagnostics is not None:
            await self.page_diagnostics.close()
        if self.network_recorder is not None:
            await self.network_recorder.close()
        self.session = session
        if borrowed:
            self._borrowed_target_ids.add(session.target_id)
        else:
            self._owned_target_ids.add(session.target_id)
        self._candidate_cache.clear()
        self._last_observation_fingerprint = None
        self.network_recorder = CdpNetworkRecorder(
            session,
            capture=self.network_capture,
            traffic=self.network_traffic,
        )
        await self.network_recorder.start(session.target_id)
        self.page_diagnostics = CdpPageDiagnostics(session)
        await self.page_diagnostics.start()
        await self.operation_recorder.start(session)
        await self._start_frame_registry(session)
        await self._reapply_emulation(session)
        logger.info(
            "已接管当前点击打开的新标签页",
            extra={"target_id": session.target_id},
        )

    async def _reapply_emulation(self, session: CdpTargetSession) -> None:
        """新标签页不继承任何模拟覆盖，切页后必须整套重施。"""

        if self.emulation_state is None:
            return
        try:
            await apply_emulation_state(session, self.emulation_state)
        except CdpCommandError as exc:
            logger.warning(
                "新页面重施环境模拟失败",
                extra={"target_id": session.target_id, "reason": str(exc)},
            )

    async def apply_emulation(self, state: EmulationState | None) -> dict[str, Any]:
        """应用或清除环境模拟，并回读页面实际生效的环境。"""

        session = self._require_session()
        if state is None:
            await clear_emulation_state(session)
            self.emulation_state = None
        else:
            await apply_emulation_state(session, state)
            self.emulation_state = state
        self._candidate_cache.clear()
        self._last_observation_fingerprint = None
        return await read_effective_emulation(session)

    def current_emulation(self) -> dict[str, Any]:
        return self.emulation_state.public_dict() if self.emulation_state else {}

    async def _start_download_tracker(self) -> None:
        """下载事件挂在浏览器连接上，整次任务共用一个跟踪器。"""

        if self.download_tracker is not None:
            return
        connection = getattr(self.browser, "connection", None)
        if connection is None:
            return
        tracker = DownloadTracker(connection, self.artifact_root / "downloads")
        try:
            await tracker.start()
        except CdpCommandError as exc:
            logger.warning(
                "启用浏览器下载接管失败，下载相关工具将不可用",
                extra={"cdp_method": exc.method, "cdp_error_code": exc.error_code},
            )
            return
        self.download_tracker = tracker

    async def list_downloads(self, *, limit: int = 20) -> list[dict[str, Any]]:
        tracker = self.download_tracker
        if tracker is None:
            return []
        return tracker.list_downloads(limit=limit)

    async def wait_for_download(
        self,
        *,
        suggested_filename: str | None = None,
        url_contains: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        tracker = self.download_tracker
        if tracker is None:
            raise RuntimeError("当前浏览器没有启用下载跟踪")
        return await tracker.wait_for_download(
            suggested_filename=suggested_filename,
            url_contains=url_contains,
            timeout_seconds=timeout_seconds,
        )

    async def current_page_url(self) -> str:
        session = self.session
        if session is None:
            return self._last_known_url
        return await self._current_page_url(session)

    async def fill_fields(self, fields: Sequence[FormField]) -> list[dict[str, Any]]:
        """按序写入多个表单字段，逐字段回读校验。

        一个字段失败不打断其余字段：调用方需要知道整张表单里到底哪几格没写进去，
        中途停下只会让下一次调用重复已经成功的部分。
        """

        page_session = self._require_session()
        results: list[dict[str, Any]] = []
        async with self._action_lock:
            for field in fields:
                try:
                    if field.locator is not None:
                        frame = await self._resolve_frame(field.locator)
                        object_id = await resolve_locator_object(frame, field.locator)
                        session: CdpTargetSession = frame.session
                    else:
                        _, _, object_id = await self._resolve_target(field.target_id or "")
                        session = page_session
                    results.append(await apply_form_field(session, page_session, object_id, field))
                except (TargetNotFoundError, CdpCommandError) as exc:
                    results.append({**field.describe(), "filled": False, "reason": str(exc)})
        self._last_observation_fingerprint = None
        return results

    async def drag_to_element(
        self,
        *,
        source_target_id: str | None = None,
        source_locator: LocatorRecipe | None = None,
        target_target_id: str | None = None,
        target_locator: LocatorRecipe | None = None,
        steps: int = 12,
        step_delay_ms: int = 16,
    ) -> dict[str, Any]:
        """把源元素拖到目标元素上，自动识别原生拖放还是鼠标拖放。"""

        async with self._action_lock:
            page_session = self._require_session()
            source, source_box = await self._endpoint(source_target_id, source_locator)
            target, target_box = await self._endpoint(target_target_id, target_locator)
            outcome = await drag_between_points(
                page_session,
                source_box.center,
                target_box.center,
                steps=steps,
                step_delay_ms=step_delay_ms,
            )
        self._candidate_cache.clear()
        self._last_observation_fingerprint = None
        return {
            "source": source.name,
            "target": target.name,
            "source_risk": source.drag_risk.value if source.drag_risk else "unknown",
            **outcome,
        }

    async def _endpoint(
        self,
        target_id: str | None,
        locator: LocatorRecipe | None,
    ) -> tuple[CandidateTarget, BoundingBox]:
        if locator is not None:
            frame = await self._resolve_frame(locator)
            # resolve_explicit_locator 返回的 box 已换算到主框架视口，可直接派发。
            candidate, box, _ = await resolve_explicit_locator(frame, locator)
            return candidate, box
        candidate, box, _ = await self._resolve_target(target_id or "")
        return candidate, box

    async def save_page_pdf(self, *, label: str, params: dict[str, Any]) -> dict[str, Any]:
        return await export_page_pdf(
            self._require_session(), self.artifact_root / "pdf", label=label, params=params
        )

    async def measure_performance(
        self,
        *,
        reload_page: bool = False,
        settle_seconds: float = 0.5,
    ) -> dict[str, Any]:
        """采集性能数据；要拿到 LCP 必须先注入采集器再重载。"""

        session = self._require_session()
        await install_performance_collector(session)
        if reload_page:
            async with expect_navigation_settled(session, timeout_seconds=30) as settle:
                await session.call("Page.reload", {"ignoreCache": False})
                await settle()
            self._candidate_cache.clear()
            self._last_observation_fingerprint = None
        metrics = await read_performance_metrics(session, settle_seconds=settle_seconds)
        metrics["counters"] = await read_performance_counters(session)
        metrics["reloaded"] = reload_page
        return metrics

    async def export_storage_state(self, *, urls: list[str]) -> dict[str, Any]:
        session = self._require_session()
        frame = await (await self._require_frames()).resolve(None)
        return await export_session_state(session, frame, urls=urls)

    async def import_storage_state(
        self,
        state: dict[str, Any],
        *,
        allowed_origins: set[str],
        clear_existing: bool = False,
    ) -> dict[str, Any]:
        session = self._require_session()
        frame = await (await self._require_frames()).resolve(None)
        outcome = await import_session_state(
            session,
            frame,
            state,
            allowed_origins=allowed_origins,
            clear_existing=clear_existing,
        )
        self._last_observation_fingerprint = None
        return outcome

    async def wait_for(self, condition: ExpectedCondition) -> dict[str, Any]:
        """独立等待一个可验证条件；轮询由 verify 内部完成，不消耗模型调用。"""

        started = time.perf_counter()
        verification = await self.verify(condition)
        return {
            "satisfied": verification.success,
            "message": verification.reason,
            "waited_seconds": round(time.perf_counter() - started, 3),
        }

    def dialog_policy(self) -> dict[str, dict[str, Any]]:
        return self.browser.dialog_supervisor.effective_policy()

    def dialog_records(self) -> list[dict[str, Any]]:
        return [record.public_dict() for record in self.browser.dialog_supervisor.records()]

    def set_dialog_rule(
        self,
        action: str,
        *,
        prompt_text: str = "",
        once: bool,
        kinds: tuple[str, ...] | None = None,
    ) -> None:
        self.browser.dialog_supervisor.set_rule(
            action,
            prompt_text=prompt_text,
            once=once,
            kinds=tuple(kinds) if kinds else DIALOG_KINDS,
        )

    async def read_cookies(
        self,
        url: str,
        *,
        names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        session = self._require_session()
        return await read_page_cookies(session, url, names=names)

    async def set_cookie(
        self,
        *,
        name: str,
        value: str,
        url: str,
        path: str = "/",
        domain: str | None = None,
        http_only: bool = False,
        secure: bool = False,
        expires: float | None = None,
    ) -> dict[str, Any]:
        session = self._require_session()
        return await set_page_cookie(
            session,
            name=name,
            value=value,
            url=url,
            path=path,
            domain=domain,
            http_only=http_only,
            secure=secure,
            expires=expires,
        )

    async def read_web_storage(
        self,
        *,
        storage_kind: str,
        key: str | None = None,
        frame_id: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_active_page()
        frame = await self._resolve_frame_by_id(frame_id)
        return await read_frame_web_storage(frame, storage_kind=storage_kind, key=key)

    async def write_web_storage(
        self,
        *,
        storage_kind: str,
        key: str,
        value: str | None = None,
        frame_id: str | None = None,
        remove: bool = False,
    ) -> dict[str, Any]:
        await self._ensure_active_page()
        frame = await self._resolve_frame_by_id(frame_id)
        return await write_frame_web_storage(
            frame,
            storage_kind=storage_kind,
            key=key,
            value=value,
            remove=remove,
        )

    async def _resolve_frame_by_id(self, frame_id: str | None) -> FrameHandle:
        registry = await self._require_frames()
        return await registry.resolve(frame_id)

    async def _start_frame_registry(self, session: CdpTargetSession) -> None:
        """为当前页面建立帧注册表；失败时退化为只支持主框架。"""

        if self._frames is not None:
            self._frames.close()
            self._frames = None
        registry = FrameRegistry(session)
        try:
            await registry.start()
        except (CdpCommandError, CdpDisconnectedError) as exc:
            logger.warning(
                "页面级 iframe 自动附着失败，跨站 iframe 将不可操作",
                extra={"target_id": session.target_id, "reason": str(exc)},
            )
            registry.close()
            return
        self._frames = registry

    async def _require_frames(self) -> FrameRegistry:
        registry = self._frames
        session = self._require_session()
        if registry is None or registry.page_session is not session:
            await self._start_frame_registry(session)
            registry = self._frames
        if registry is None:
            raise TargetNotFoundError("当前页面无法建立 iframe 帧注册表")
        return registry

    async def list_frames(self) -> list[dict[str, Any]]:
        """列出当前页面的主框架与全部子帧，供后续按 frame_id 定位。"""

        await self._ensure_active_page()
        registry = await self._require_frames()
        return [descriptor.as_dict() for descriptor in await registry.list_frames()]

    async def _resolve_frame(self, locator: LocatorRecipe | None) -> FrameHandle:
        """把定位器上的 frame_id 解析成执行面；没有 frame_id 时就是主框架。"""

        registry = await self._require_frames()
        return await registry.resolve(locator.frame_id if locator is not None else None)

    async def _ensure_active_page(self, *, fallback_url: str = "") -> bool:
        """在用户关闭标签页或最后一个窗口后，于原 CDP 连接中恢复任务页面。"""

        session = self.session
        if session is None or self._session_is_active(session):
            return False
        async with self._session_recovery_lock:
            session = self.session
            if session is None or self._session_is_active(session):
                return False
            takeover_requested = self.browser_config.session_mode is BrowserSessionMode.TAKEOVER
            if takeover_requested:
                recovered = await self.browser.claim_existing_page()
                if recovered is None:
                    raise TargetNotFoundError("接管的标签页已关闭，当前没有其他可借用页面")
                recovery_url = ""
            else:
                recovery_url = self._last_known_url or fallback_url or "about:blank"
                recovered = await self.browser.create_page(self.context_id, recovery_url)
            if takeover_requested:
                await self._adopt_page_session(recovered, borrowed=True)
            else:
                await self._adopt_page_session(recovered)
            self.browser.remember_target(recovered.target_id)
            self._page_recovered_since_observation = True
            logger.info(
                "检测到任务页面已关闭，已恢复可用页面",
                extra={
                    "target_id": recovered.target_id,
                    "recovery_url": recovery_url,
                    "session_mode": self.browser_config.session_mode.value,
                },
            )
            return True

    def _session_is_active(self, session: CdpTargetSession) -> bool:
        checker = getattr(self.browser, "is_session_active", None)
        if checker is None:
            return True
        # 测试替身和嵌入式驱动可直接注入 Session；真实 CDP 会话启动后 connection 一定存在。
        if isinstance(self.browser, CdpBrowser) and self.browser.connection is None:
            return True
        return bool(checker(session))

    @staticmethod
    async def _dispatch_click(
        session: CdpTargetSession,
        box: BoundingBox,
        command: ActionCommand,
    ) -> None:
        x, y = box.center
        await dispatch_pointer_click(
            session,
            x,
            y,
            button=command.pointer_button,
            click_count=command.click_count,
        )

    @staticmethod
    def _opens_new_page(candidate: CandidateTarget) -> bool:
        if candidate.role != "link" or not candidate.recipe.value:
            return False
        try:
            locator = json.loads(candidate.recipe.value)
        except (json.JSONDecodeError, TypeError):
            return False
        attributes = locator.get("attrs") if isinstance(locator, dict) else None
        return (
            isinstance(attributes, dict)
            and str(attributes.get("target", "")).casefold() == "_blank"
        )

    @staticmethod
    def _is_page_opened_by(event: CdpEvent, opener_target_id: str) -> bool:
        target_info = event.params.get("targetInfo")
        return (
            isinstance(target_info, dict)
            and target_info.get("type") == "page"
            and target_info.get("openerId") == opener_target_id
        )

    @staticmethod
    async def _drag_target(
        element_session: CdpTargetSession,
        page_session: CdpTargetSession,
        candidate: CandidateTarget,
        box: BoundingBox,
        object_id: str,
        command: ActionCommand,
    ) -> dict[str, Any]:
        if (
            command.drag_risk is DragRiskClass.BUSINESS
            and candidate.drag_risk is DragRiskClass.BUSINESS
            and is_native_range(candidate)
        ):
            return await set_native_range_from_drag(element_session, box, object_id, command)

        center_x, center_y = box.center
        points = tuple(
            (center_x + point.dx, center_y + point.dy, point.delay_ms)
            for point in command.trajectory
        )
        await dispatch_drag(page_session, points)
        return {"执行方式": "pointer"}

    async def _drag_viewport(
        self,
        session: CdpTargetSession,
        command: ActionCommand,
    ) -> dict[str, Any]:
        if command.observation_fingerprint != self._last_observation_fingerprint:
            raise TargetNotFoundError("视觉拖拽绑定的页面观察已经失效")
        current_screenshot = await self._capture_screenshot_bytes()
        current_fingerprint = hashlib.sha256(current_screenshot).hexdigest()
        dynamic_visual_frame = False
        if command.screenshot_fingerprint != current_fingerprint:
            if not command.allow_dynamic_visual_frame:
                raise TargetNotFoundError("视觉拖拽绑定的截图已经变化，请重新观察")
            refreshed = await self.observe(force=True)
            if refreshed.fingerprint != command.observation_fingerprint:
                raise TargetNotFoundError("动态视觉帧重新观察后页面观察已经变化")
            dynamic_visual_frame = True
            logger.info(
                "安全挑战截图包含动态像素，语义观察稳定，继续执行有限拖拽",
                extra={"action_id": command.action_id},
            )
        metrics = await session.call("Page.getLayoutMetrics")
        viewport = metrics.get("cssVisualViewport") or metrics.get("cssLayoutViewport")
        if not isinstance(viewport, dict):
            raise RuntimeError("浏览器未返回可用的 CSS 视口尺寸")
        width = float(viewport.get("clientWidth", 0))
        height = float(viewport.get("clientHeight", 0))
        if width <= 0 or height <= 0:
            raise RuntimeError("浏览器返回的 CSS 视口尺寸无效")
        points = tuple(
            (point.x_ratio * width, point.y_ratio * height, point.delay_ms)
            for point in command.visual_trajectory
        )
        start_hit = await visual_point_diagnostic(session, points[0][0], points[0][1])
        if start_hit.get("诊断可用") is True and (
            start_hit.get("命中") is not True or start_hit.get("指针事件") == "none"
        ):
            raise TargetNotFoundError("视觉拖拽起点未命中可接收指针事件的页面元素")
        pointer_feedback = await dispatch_drag(session, points, approach=True)
        end_hit = await visual_point_diagnostic(session, points[-1][0], points[-1][1])
        pixels_changed = await visual_pixels_changed(session, current_fingerprint)
        return {
            "执行方式": "pointer",
            "可视指针反馈": pointer_feedback,
            "动态视觉帧": dynamic_visual_frame,
            "起点命中": start_hit,
            "释放点命中": end_hit,
            "拖后像素变化": pixels_changed,
        }

    async def _click_viewport(
        self,
        session: CdpTargetSession,
        command: ActionCommand,
    ) -> None:
        if command.observation_fingerprint != self._last_observation_fingerprint:
            raise TargetNotFoundError("视觉点击绑定的页面观察已经失效")
        current_screenshot = await self._capture_screenshot_bytes()
        current_fingerprint = hashlib.sha256(current_screenshot).hexdigest()
        if command.screenshot_fingerprint != current_fingerprint:
            raise TargetNotFoundError("视觉点击绑定的截图已经变化，请重新观察")
        metrics = await session.call("Page.getLayoutMetrics")
        viewport = metrics.get("cssVisualViewport") or metrics.get("cssLayoutViewport")
        if not isinstance(viewport, dict):
            raise RuntimeError("浏览器未返回可用的 CSS 视口尺寸")
        width = float(viewport.get("clientWidth", 0))
        height = float(viewport.get("clientHeight", 0))
        if width <= 0 or height <= 0:
            raise RuntimeError("浏览器返回的 CSS 视口尺寸无效")
        x = float(command.visual_x_ratio or 0) * width
        y = float(command.visual_y_ratio or 0) * height
        await self._dispatch_click(session, BoundingBox(x, y, 0, 0), command)

    async def _capture_visual_region(
        self,
        session: CdpTargetSession,
        command: ActionCommand,
    ) -> Path:
        if command.observation_fingerprint != self._last_observation_fingerprint:
            raise TargetNotFoundError("视觉区域观察绑定的页面观察已经失效")
        current_screenshot = await self._capture_screenshot_bytes()
        current_fingerprint = hashlib.sha256(current_screenshot).hexdigest()
        if command.screenshot_fingerprint != current_fingerprint:
            refreshed = await self.observe(force=True)
            if refreshed.fingerprint != command.observation_fingerprint:
                raise TargetNotFoundError("动态视觉帧重新观察后页面观察已经变化")
        metrics = await session.call("Page.getLayoutMetrics")
        viewport = metrics.get("cssVisualViewport") or metrics.get("cssLayoutViewport")
        if not isinstance(viewport, dict):
            raise RuntimeError("浏览器未返回可用的 CSS 视口尺寸")
        width = float(viewport.get("clientWidth", 0))
        height = float(viewport.get("clientHeight", 0))
        page_x = float(viewport.get("pageX", 0))
        page_y = float(viewport.get("pageY", 0))
        if width <= 0 or height <= 0:
            raise RuntimeError("浏览器返回的 CSS 视口尺寸无效")

        x_ratio, y_ratio, width_ratio, height_ratio = command.visual_clip or (0, 0, 1, 1)
        clip = {
            "x": page_x + x_ratio * width,
            "y": page_y + y_ratio * height,
            "width": width_ratio * width,
            "height": height_ratio * height,
            "scale": 2,
        }
        result = await session.call(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
                "clip": clip,
            },
        )
        data = result.get("data")
        if not isinstance(data, str):
            raise RuntimeError("浏览器未返回视觉区域截图数据")
        image_bytes = base64.b64decode(data)
        return self._write_evidence_bytes(f"visual-region-{command.action_id}", image_bytes)

    async def _navigate(self, url: str, timeout_seconds: float) -> None:
        session = self._require_session()
        async with session.connection.expect_event(
            "Page.loadEventFired",
            session_id=session.session_id,
        ) as loaded:
            result = await session.call("Page.navigate", {"url": url})
            if result.get("errorText"):
                raise RuntimeError(f"页面导航失败：{result['errorText']}")
            try:
                await asyncio.wait_for(loaded, timeout=timeout_seconds)
            except TimeoutError as exc:
                raise RuntimeError("等待页面加载完成超时") from exc
        if url and url != "about:blank":
            self._last_known_url = url

    async def _build_candidates(
        self,
        nodes: list[Any],
        dom_root: dict[str, Any] | None,
        pointer_nodes: list[Any],
        *,
        page_drag_risk: DragRiskClass,
        page_drag_risk_reasons: tuple[str, ...],
        viewport_height: float | None = None,
    ) -> list[CandidateTarget]:
        session = self._require_session()
        semaphore = asyncio.Semaphore(12)
        dom_nodes = self._iter_dom_nodes(dom_root)
        dom_by_backend_id = {
            item["backendNodeId"]: item
            for item in dom_nodes
            if isinstance(item.get("backendNodeId"), int)
        }

        async def build_ax(node: dict[str, Any]) -> CandidateTarget | None:
            role = self._ax_value(node.get("role"))
            name = self._ax_value(node.get("name"))
            backend_node_id = node.get("backendDOMNodeId")
            if role not in _ACTIONABLE_ROLES or not isinstance(backend_node_id, int):
                return None
            properties = node.get("properties", [])
            disabled = any(
                item.get("name") == "disabled" and self._ax_value(item.get("value")) == "true"
                for item in properties
                if isinstance(item, dict)
            )
            async with semaphore:
                box = await self._box_for_backend_node(backend_node_id)
            dom_node = dom_by_backend_id.get(backend_node_id, {})
            tag = str(dom_node.get("nodeName", "")).lower()
            attributes = self._dom_attributes(dom_node.get("attributes"))
            stable_attributes = {
                key: value for key, value in attributes.items() if key in _STABLE_ATTRIBUTE_NAMES
            }
            drag_risk, drag_risk_reasons = self._classify_candidate_drag_risk(
                role=role,
                tag=tag,
                attributes=attributes,
                page_drag_risk=page_drag_risk,
                page_drag_risk_reasons=page_drag_risk_reasons,
            )
            confidence = 0.95 if name else 0.72
            reasons = ("无障碍角色与名称匹配",) if name else ("无障碍角色匹配",)
            target_id = f"{session.target_id}:{session.observation_version}:{backend_node_id}"
            return CandidateTarget(
                target_id=target_id,
                role=role,
                name=name,
                text=name,
                confidence=confidence,
                reasons=reasons,
                recipe=LocatorRecipe(
                    strategy="ax_backend_node",
                    role=role,
                    name=name,
                    value=self._serialize_locator_value(
                        tag=tag,
                        role=role,
                        name=name,
                        text=name,
                        attributes=stable_attributes,
                    ),
                    backend_node_id=backend_node_id,
                ),
                box=box,
                disabled=disabled,
                drag_risk=drag_risk,
                drag_risk_reasons=drag_risk_reasons,
            )

        async def build_dom(node: dict[str, Any]) -> CandidateTarget | None:
            backend_node_id = node.get("backendNodeId")
            tag = str(node.get("nodeName", "")).lower()
            attributes = self._dom_attributes(node.get("attributes"))
            explicit_role = attributes.get("role", "").lower()
            if not isinstance(backend_node_id, int) or (
                tag not in _DOM_ACTIONABLE_TAGS and explicit_role not in _ACTIONABLE_ROLES
            ):
                return None
            role = self._dom_role(tag, attributes)
            if role not in _ACTIONABLE_ROLES:
                return None
            text = self._dom_text(node)
            name = self._dom_name(tag, attributes, text)
            async with semaphore:
                box = await self._box_for_backend_node(backend_node_id)
            if box is None or box.area < 1:
                return None
            stable_attributes = {
                key: value for key, value in attributes.items() if key in _STABLE_ATTRIBUTE_NAMES
            }
            drag_risk, drag_risk_reasons = self._classify_candidate_drag_risk(
                role=role,
                tag=tag,
                attributes=attributes,
                page_drag_risk=page_drag_risk,
                page_drag_risk_reasons=page_drag_risk_reasons,
            )
            confidence = 0.68 if name else 0.56
            reasons = ("DOM 结构补充候选",)
            target_id = f"{session.target_id}:{session.observation_version}:{backend_node_id}"
            return CandidateTarget(
                target_id=target_id,
                role=role,
                name=name,
                text=text,
                confidence=confidence,
                reasons=reasons,
                recipe=LocatorRecipe(
                    strategy="dom_backend_node",
                    role=role,
                    name=name,
                    value=self._serialize_locator_value(
                        tag=tag,
                        role=role,
                        name=name,
                        text=text,
                        attributes=stable_attributes,
                    ),
                    backend_node_id=backend_node_id,
                ),
                box=box,
                disabled=attributes.get("disabled") is not None
                or attributes.get("aria-disabled") == "true",
                drag_risk=drag_risk,
                drag_risk_reasons=drag_risk_reasons,
            )

        ax_tasks = [build_ax(node) for node in nodes if isinstance(node, dict)]
        ax_built = await asyncio.gather(*ax_tasks, return_exceptions=True)
        candidates = [item for item in ax_built if isinstance(item, CandidateTarget)]
        seen_backend_ids = {
            item.recipe.backend_node_id
            for item in candidates
            if item.recipe.backend_node_id is not None
        }
        dom_tasks = [build_dom(node) for node in dom_nodes]
        dom_built = await asyncio.gather(*dom_tasks, return_exceptions=True)
        for item in dom_built:
            if not isinstance(item, CandidateTarget):
                continue
            backend_node_id = item.recipe.backend_node_id
            if backend_node_id is None or backend_node_id in seen_backend_ids:
                continue
            seen_backend_ids.add(backend_node_id)
            candidates.append(item)

        for raw_pointer in pointer_nodes:
            if not isinstance(raw_pointer, dict):
                continue
            selector = self._normalize_text(raw_pointer.get("selector"))
            text = self._normalize_text(raw_pointer.get("text"))[:200]
            if not selector or not text:
                continue
            box_value = raw_pointer.get("box")
            if not isinstance(box_value, dict):
                continue
            try:
                box = BoundingBox(
                    float(box_value.get("x", 0)),
                    float(box_value.get("y", 0)),
                    float(box_value.get("width", 0)),
                    float(box_value.get("height", 0)),
                )
            except (TypeError, ValueError):
                continue
            if box.area < 1:
                continue
            raw_attributes = raw_pointer.get("attrs")
            attributes = (
                {
                    str(key): self._normalize_text(value)
                    for key, value in raw_attributes.items()
                    if isinstance(key, str) and isinstance(value, str) and value
                }
                if isinstance(raw_attributes, dict)
                else {}
            )
            name = _pointer_candidate_name(raw_pointer.get("name"), text, attributes)
            role = self._normalize_text(raw_pointer.get("role")).casefold()
            if role not in _ACTIONABLE_ROLES:
                role = "button"
            locator_value = self._serialize_locator_value(
                tag=self._normalize_text(raw_pointer.get("tag")).casefold(),
                role=role,
                name=name,
                text=text,
                attributes=attributes,
            )
            locator = json.loads(locator_value)
            locator["selector"] = selector
            selector_hash = hashlib.sha256(selector.encode("utf-8")).hexdigest()[:16]
            candidates.append(
                CandidateTarget(
                    target_id=(
                        f"{session.target_id}:{session.observation_version}:pointer-{selector_hash}"
                    ),
                    role=role,
                    name=name,
                    text=text,
                    confidence=0.82,
                    reasons=("可见 DOM 元素具有明确的 pointer 交互光标",),
                    recipe=LocatorRecipe(
                        strategy="pointer_css",
                        role=role,
                        name=name,
                        value=json.dumps(locator, ensure_ascii=False, sort_keys=True),
                    ),
                    box=box,
                    disabled=raw_pointer.get("disabled") is True,
                    drag_risk=DragRiskClass.UNKNOWN,
                    drag_risk_reasons=("pointer 交互元素不属于可证明的原生业务滑块",),
                )
            )
        # 截断前先按智能体的需要排：可输入控件 > 其它控件 > 链接，视口内优先，再看置信度。
        return rank_candidates(candidates, viewport_height=viewport_height)[
            :MAX_OBSERVATION_CANDIDATES
        ]

    @staticmethod
    def _pointer_target_values(result: dict[str, Any]) -> list[Any]:
        value = result.get("result", {}).get("value")
        return value if isinstance(value, list) else []

    @staticmethod
    def _classify_page_drag_risk(
        url: str,
        title: str,
        text: str,
    ) -> tuple[DragRiskClass, tuple[str, ...]]:
        visible_text = f"{title} {text}".casefold()
        if any(marker in visible_text for marker in _SECURITY_CHALLENGE_MARKERS):
            return DragRiskClass.SECURITY, ("页面包含明确的人机验证信号",)
        authentication_context = any(
            marker in visible_text for marker in _AUTHENTICATION_ACTION_MARKERS
        ) and any(marker in visible_text for marker in _AUTHENTICATION_CREDENTIAL_MARKERS)
        if authentication_context:
            return DragRiskClass.SECURITY, ("页面处于认证上下文，拖拽按安全挑战处理",)
        lowered_url = url.casefold()
        if any(marker in lowered_url for marker in _SECURITY_URL_MARKERS):
            return DragRiskClass.SECURITY, ("页面地址属于认证或挑战路径",)
        return DragRiskClass.UNKNOWN, ("页面没有足够信息证明视觉拖拽属于普通业务控件",)

    @staticmethod
    def _classify_candidate_drag_risk(
        *,
        role: str,
        tag: str,
        attributes: dict[str, str],
        page_drag_risk: DragRiskClass,
        page_drag_risk_reasons: tuple[str, ...],
    ) -> tuple[DragRiskClass, tuple[str, ...]]:
        if page_drag_risk is DragRiskClass.SECURITY:
            return page_drag_risk, page_drag_risk_reasons
        if role == "slider" and tag == "input" and attributes.get("type") == "range":
            return DragRiskClass.BUSINESS, ("目标是原生 input[type=range] 业务控件",)
        return DragRiskClass.UNKNOWN, ("目标不是可证明的原生业务滑块",)

    async def _resolve_target(
        self,
        target_id: str,
    ) -> tuple[CandidateTarget, BoundingBox, str]:
        candidate = self._candidate_cache.get(target_id)
        if not candidate:
            raise TargetNotFoundError("目标区域不存在或页面观察已经过期")
        session = self._require_session()
        expected_prefix = f"{session.target_id}:{session.observation_version}:"
        if not target_id.startswith(expected_prefix):
            raise TargetNotFoundError("页面状态已变化，必须重新观察后再定位目标区域")
        if candidate.disabled:
            raise TargetNotFoundError("目标区域处于禁用状态")

        if candidate.recipe.strategy == "pointer_css":
            locator = self._parse_locator_value(candidate.recipe.value)
            selector = locator.get("selector")
            if not isinstance(selector, str) or not selector:
                raise TargetNotFoundError("pointer 交互目标缺少可执行定位配方")
            resolved = await session.call(
                "Runtime.evaluate",
                {
                    "expression": f"document.querySelector({json.dumps(selector)})",
                    "returnByValue": False,
                },
            )
            object_id = resolved.get("result", {}).get("objectId")
            if not isinstance(object_id, str):
                raise TargetNotFoundError("pointer 交互目标已经不存在")
            state_result = await session.call(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": _POINTER_TARGET_STATE_SCRIPT,
                    "returnByValue": True,
                },
            )
            state = state_result.get("result", {}).get("value")
            if not isinstance(state, dict):
                raise TargetNotFoundError("无法重新确认 pointer 交互目标状态")
            current_text = self._normalize_text(state.get("text"))
            if current_text != self._normalize_text(candidate.text):
                raise TargetNotFoundError("pointer 交互目标文本已经变化，必须重新观察")
            if state.get("disabled") is True:
                raise TargetNotFoundError("pointer 交互目标处于禁用状态")
            await session.call("DOM.scrollIntoViewIfNeeded", {"objectId": object_id})
            box = await self._box_for_object_id(object_id)
        else:
            backend_node_id = candidate.recipe.backend_node_id
            if backend_node_id is None:
                raise TargetNotFoundError("目标定位配方缺少浏览器节点 ID")
            await session.call(
                "DOM.scrollIntoViewIfNeeded",
                {"backendNodeId": backend_node_id},
            )
            box = await self._box_for_backend_node(backend_node_id)
            resolved = await session.call(
                "DOM.resolveNode",
                {"backendNodeId": backend_node_id},
            )
            object_id = resolved.get("object", {}).get("objectId")
            if not isinstance(object_id, str):
                raise TargetNotFoundError("无法解析目标区域")
        if box is None or box.area < 1:
            raise TargetNotFoundError("目标区域不可见或尺寸无效")
        x, y = box.center
        hit_test = await session.call(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": _HIT_TEST_SCRIPT,
                "arguments": [{"value": x}, {"value": y}],
                "returnByValue": True,
            },
        )
        if hit_test.get("result", {}).get("value") is not True:
            raise TargetNotFoundError("目标区域被遮挡或不能接收点击")
        return candidate, box, object_id

    async def _box_for_backend_node(self, backend_node_id: int) -> BoundingBox | None:
        try:
            result = await self._require_session().call(
                "DOM.getBoxModel",
                {"backendNodeId": backend_node_id},
                timeout_seconds=3,
            )
        except Exception:
            return None
        model = result.get("model", {})
        quad = model.get("border") or model.get("content")
        if not isinstance(quad, list) or len(quad) < 8:
            return None
        xs = [float(quad[index]) for index in range(0, 8, 2)]
        ys = [float(quad[index]) for index in range(1, 8, 2)]
        return BoundingBox(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    async def _box_for_object_id(self, object_id: str) -> BoundingBox | None:
        try:
            result = await self._require_session().call(
                "DOM.getBoxModel",
                {"objectId": object_id},
                timeout_seconds=3,
            )
        except Exception:
            return None
        model = result.get("model", {})
        quad = model.get("border") or model.get("content")
        if not isinstance(quad, list) or len(quad) < 8:
            return None
        xs = [float(quad[index]) for index in range(0, 8, 2)]
        ys = [float(quad[index]) for index in range(1, 8, 2)]
        return BoundingBox(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    @staticmethod
    def _ax_value(value: Any) -> str:
        if isinstance(value, dict):
            raw = value.get("value")
            return str(raw) if raw is not None else ""
        return ""

    @classmethod
    def _dom_attributes(cls, raw_attributes: Any) -> dict[str, str]:
        if not isinstance(raw_attributes, list):
            return {}
        attributes: dict[str, str] = {}
        for index in range(0, len(raw_attributes) - 1, 2):
            key = raw_attributes[index]
            value = raw_attributes[index + 1]
            if isinstance(key, str) and isinstance(value, str):
                attributes[key.lower()] = cls._normalize_text(value)
        return attributes

    @classmethod
    def _dom_role(cls, tag: str, attributes: dict[str, str]) -> str:
        explicit_role = attributes.get("role", "").lower()
        if explicit_role in _ACTIONABLE_ROLES:
            return explicit_role
        if tag == "a" and attributes.get("href"):
            return "link"
        if tag == "button" or tag == "summary":
            return "button"
        if tag == "select":
            return "combobox"
        if tag == "textarea":
            return "textbox"
        if tag == "option":
            return "option"
        if tag != "input":
            return ""
        input_type = attributes.get("type", "text").lower()
        if input_type in {"hidden", "file"}:
            return ""
        if input_type in {"button", "submit", "reset"}:
            return "button"
        if input_type == "checkbox":
            return "checkbox"
        if input_type == "radio":
            return "radio"
        if input_type == "range":
            return "slider"
        if input_type == "search":
            return "searchbox"
        if input_type == "number":
            return "spinbutton"
        return "textbox"

    @classmethod
    def _dom_name(cls, tag: str, attributes: dict[str, str], text: str) -> str:
        for key in ("aria-label", "placeholder", "title", "value", "alt", "name", "id"):
            value = attributes.get(key)
            if value:
                return value
        if tag in {"a", "button", "option", "summary"} and text:
            return text
        return text

    @classmethod
    def _dom_text(cls, node: dict[str, Any]) -> str:
        texts: list[str] = []
        stack = [node]
        while stack:
            current = stack.pop()
            node_name = str(current.get("nodeName", "")).lower()
            node_value = current.get("nodeValue")
            if node_name == "#text" and isinstance(node_value, str):
                normalized = cls._normalize_text(node_value)
                if normalized:
                    texts.append(normalized)
            children = current.get("children")
            if isinstance(children, list):
                stack.extend(reversed([child for child in children if isinstance(child, dict)]))
        return cls._normalize_text(" ".join(texts))

    @classmethod
    def _iter_dom_nodes(cls, root: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(root, dict):
            return []
        nodes: list[dict[str, Any]] = []
        stack = [root]
        while stack:
            current = stack.pop()
            nodes.append(current)
            children = current.get("children")
            if isinstance(children, list):
                stack.extend(reversed([child for child in children if isinstance(child, dict)]))
        return nodes

    @classmethod
    def _serialize_locator_value(
        cls,
        *,
        tag: str,
        role: str,
        name: str,
        text: str,
        attributes: dict[str, str],
    ) -> str:
        return json.dumps(
            {
                "attrs": attributes,
                "name": cls._normalize_text(name),
                "role": role,
                "tag": tag,
                "text": cls._normalize_text(text)[:120],
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def _parse_locator_value(cls, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @classmethod
    def _matches_locator_recipe(
        cls,
        previous: CandidateTarget,
        current: CandidateTarget,
    ) -> bool:
        if previous.role and current.role != previous.role:
            return False

        previous_locator = cls._parse_locator_value(previous.recipe.value)
        current_locator = cls._parse_locator_value(current.recipe.value)
        previous_attrs = previous_locator.get("attrs", {})
        current_attrs = current_locator.get("attrs", {})
        if isinstance(previous_attrs, dict) and isinstance(current_attrs, dict):
            stable_pairs = [
                (key, value)
                for key, value in previous_attrs.items()
                if isinstance(key, str) and isinstance(value, str) and value
            ]
            if stable_pairs:
                return all(current_attrs.get(key) == value for key, value in stable_pairs)

        stable_name = cls._normalize_text(previous.recipe.name or previous.name)
        if stable_name:
            return cls._normalize_text(current.name) == stable_name

        stable_text = cls._normalize_text(previous.text)
        if stable_text:
            return cls._normalize_text(current.text) == stable_text
        return True

    @staticmethod
    def _normalize_text(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.split())

    def _require_session(self) -> CdpTargetSession:
        if not self.session:
            raise RuntimeError("浏览器页面会话尚未建立")
        return self.session

"""项目各模块之间的稳定异步协议。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from witty_browser_auto.domain.extraction import (
    CollectionExtractionResult,
    CollectionExtractionSpec,
)
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionReceipt,
    DriverCapabilities,
    ExpectedCondition,
    LocatorRecipe,
    Observation,
    VerificationResult,
)
from witty_browser_auto.domain.network_data import NetworkDataExportResult


class AutomationDriver(Protocol):
    @property
    def capabilities(self) -> DriverCapabilities: ...

    async def start(self) -> None: ...

    async def open(self, url: str) -> str: ...

    async def observe(self, *, force: bool = False) -> Observation: ...

    async def execute(self, command: ActionCommand) -> ActionReceipt: ...

    async def verify(self, condition: ExpectedCondition) -> VerificationResult: ...

    async def capture_evidence(self, label: str) -> Path: ...

    async def close(self) -> None: ...


@runtime_checkable
class PageVisibilityProvider(Protocol):
    """提供当前页面可见性，供高风险动作在锁屏或后台时暂停。"""

    async def browser_environment_snapshot(self) -> Mapping[str, Any]: ...


@runtime_checkable
class PageAttentionProvider(Protocol):
    """在等待页面可见前激活当前标签页并恢复最小化窗口。"""

    async def request_page_attention(self) -> None: ...


@runtime_checkable
class TabManagementProvider(Protocol):
    """新建、列出、切换和关闭浏览器标签页。"""

    async def list_tabs(self) -> list[dict[str, Any]]: ...

    async def open_tab(self, url: str) -> dict[str, Any]: ...

    async def switch_tab(self, target_id: str) -> dict[str, Any]: ...

    async def close_tab(self, target_id: str) -> dict[str, Any]: ...


@runtime_checkable
class FrameInspectionProvider(Protocol):
    """列出页面中的主框架与 iframe，供定位器按 frame_id 收敛作用域。"""

    async def list_frames(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class ElementInspectionProvider(Protocol):
    """按观察候选或显式定位器只读读取单个元素的语义、状态与内容。"""

    async def inspect_element(
        self,
        *,
        target_id: str | None = None,
        locator: LocatorRecipe | None = None,
        max_text_length: int = 2000,
        include_html: bool = False,
    ) -> dict[str, Any]: ...


@runtime_checkable
class ElementScreenshotProvider(Protocol):
    """只截取单个元素所在矩形，用于取证或交给视觉模型识别局部内容。"""

    async def capture_element_screenshot(
        self,
        *,
        target_id: str | None = None,
        locator: LocatorRecipe | None = None,
        label: str = "element",
        padding: float = 0.0,
    ) -> dict[str, Any]: ...


@runtime_checkable
class DownloadInspectionProvider(Protocol):
    """列出并等待浏览器下载完成的文件。"""

    async def list_downloads(self, *, limit: int = 20) -> list[dict[str, Any]]: ...

    async def wait_for_download(
        self,
        *,
        suggested_filename: str | None = None,
        url_contains: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]: ...


@runtime_checkable
class DialogControlProvider(Protocol):
    """接管 alert/confirm/prompt/beforeunload 的应答策略并留痕。"""

    def dialog_policy(self) -> dict[str, dict[str, Any]]: ...

    def dialog_records(self) -> list[dict[str, Any]]: ...

    def set_dialog_rule(
        self,
        action: str,
        *,
        prompt_text: str = "",
        once: bool,
        kinds: Sequence[str] | None = None,
    ) -> None: ...


@runtime_checkable
class StorageInspectionProvider(Protocol):
    """受控读写 Cookie 与 Web Storage，不依赖页面可见或前台焦点。"""

    async def current_page_url(self) -> str: ...

    async def read_cookies(
        self,
        url: str,
        *,
        names: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]: ...

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
    ) -> dict[str, Any]: ...

    async def read_web_storage(
        self,
        *,
        storage_kind: str,
        key: str | None = None,
        frame_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def write_web_storage(
        self,
        *,
        storage_kind: str,
        key: str,
        value: str | None = None,
        frame_id: str | None = None,
        remove: bool = False,
    ) -> dict[str, Any]: ...


@runtime_checkable
class PageDiagnosticsProvider(Protocol):
    async def diagnostic_snapshot(
        self,
        *,
        max_console: int = 20,
        max_network: int = 30,
    ) -> dict[str, Any]: ...


class StructuredDataExtractor(Protocol):
    async def inspect(
        self,
        *,
        root_selector: str = "body",
        max_candidates: int = 12,
    ) -> dict[str, Any]: ...

    async def probe_entry(self, spec: CollectionExtractionSpec) -> dict[str, Any]: ...

    async def extract(
        self,
        spec: CollectionExtractionSpec,
    ) -> CollectionExtractionResult: ...


class NetworkObserver(Protocol):
    async def start(self, surface_id: str) -> None: ...

    async def snapshot(self) -> Sequence[Mapping[str, Any]]: ...

    async def close(self) -> None: ...


class NetworkDataExtractor(Protocol):
    async def inspect(self, *, max_candidates: int = 20) -> dict[str, Any]: ...

    async def export(
        self,
        candidate_id: str,
        collection_name: str,
    ) -> NetworkDataExportResult: ...

    async def export_many(
        self,
        candidate_ids: Sequence[str],
        collection_name: str,
    ) -> NetworkDataExportResult: ...

    async def manage_route(
        self,
        operation: str,
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class AuthorizedRequestExecutor(Protocol):
    async def execute(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


@runtime_checkable
class UserInteractionSource(Protocol):
    """向运行中的任务提供用户追问，并在等待阶段触发唤醒。"""

    async def drain_messages(self, task_id: str) -> Sequence[str]: ...

    async def wait_for_activity(self, task_id: str, timeout_seconds: float) -> bool: ...

    def should_preserve_task_on_cancel(self, task_id: str) -> bool: ...

"""一步装配浏览器工具会话，供外部智能体或脚本直接使用。

`BrowserToolkit` 本身只要求 `AutomationDriver` 与 `TaskSpec`，但外部调用方通常
不了解配置、profile 隔离、网络捕获、结构化采集器和采集程序库之间的装配关系。
本模块提供与本机配置一致的装配逻辑，不引入模型决策循环：调用方拿到的是一个
已打开入口页面、可直接 `await` 工具的会话。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from witty_browser_auto.agent.crawl_tools import DEFAULT_CRAWL_AGENT
from witty_browser_auto.browser.driver import CdpAutomationDriver
from witty_browser_auto.browser.extraction import CdpDomCollectionExtractor
from witty_browser_auto.config import AppConfig, NetworkTrafficConfig
from witty_browser_auto.config_store import load_app_config
from witty_browser_auto.domain.models import ExecutionScope, TaskSpec
from witty_browser_auto.memory.background import BackgroundMemoryRuntime, shared_background_memory
from witty_browser_auto.memory.store import SqliteUrlMemoryStore
from witty_browser_auto.memory.url import normalize_url
from witty_browser_auto.network.capture import CdpNetworkCapture
from witty_browser_auto.network.inspection import NetworkTrafficInspector, TrafficSessionContext
from witty_browser_auto.network.traffic import NetworkTrafficLog
from witty_browser_auto.toolkit.facade import BrowserToolkit


def _scoped_profile_key(scope: ExecutionScope, start_url: str) -> str:
    """与同作用域任务一致的 profile 隔离键，让工具会话复用登录态。"""

    origin = normalize_url(start_url).origin
    scope_source = "\0".join((scope.project_id, scope.tenant_id, scope.account_id, origin))
    return sha256(scope_source.encode("utf-8")).hexdigest()[:24]


def _build_memory_runtime(app_config: AppConfig) -> BackgroundMemoryRuntime:
    store = SqliteUrlMemoryStore(app_config.storage.memory_database)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        runtime = BackgroundMemoryRuntime(store)
    else:
        runtime = shared_background_memory(store)
        runtime.start()
    return runtime


def build_browser_toolkit(
    start_url: str,
    *,
    goal: str = "外部浏览器工具调用会话",
    config: AppConfig | None = None,
    inputs: Mapping[str, Any] | None = None,
    allowed_origins: Sequence[str] | None = None,
    project_id: str = "toolkit",
    tenant_id: str = "default",
    account_id: str = "default",
    task_id: str | None = None,
    allow_visual_actions: bool = False,
    respect_robots: bool = False,
    min_request_interval_ms: float = 0.0,
    crawl_agent: str = DEFAULT_CRAWL_AGENT,
    read_only: bool | None = None,
) -> tuple[BrowserToolkit, CdpAutomationDriver]:
    """装配工具会话但不启动浏览器；调用方负责 `open` 与 `close` 的生命周期。

    Cookie、令牌、账号密码这类敏感值必须放进 `inputs` 并在工具参数里用键名引用，
    这样执行层脱敏才能与任务输入保护保持一致。`read_only=True` 可开启生产只读硬门控；
    配置文件或环境变量中的只读策略优先级更高，调用方不能将其关闭。
    """

    app_config = config or load_app_config()
    if read_only is not None and not isinstance(read_only, bool):
        raise ValueError("read_only 必须是布尔值")
    app_config.prepare_directories()
    resolved_task_id = task_id or f"toolkit-{uuid.uuid4().hex[:12]}"
    scope = ExecutionScope(
        project_id,
        tenant_id=tenant_id,
        account_id=account_id,
        allowed_origins=tuple(allowed_origins) if allowed_origins else (),
    )
    effective_read_only = app_config.security.read_only or bool(read_only)
    task = TaskSpec(
        resolved_task_id,
        goal,
        start_url,
        scope,
        inputs=dict(inputs or {}),
        allow_visual_actions=allow_visual_actions,
        read_only=effective_read_only,
    )
    artifact_root = app_config.storage.artifact_root / resolved_task_id
    effective_origins = scope.allowed_origins or (normalize_url(start_url).origin,)
    network_capture = (
        CdpNetworkCapture(app_config.network, artifact_root, allowed_origins=effective_origins)
        if app_config.network.enabled
        else None
    )
    traffic_log = (
        NetworkTrafficLog(
            app_config.traffic,
            # 超过内存上限的大响应落到任务产物目录，避免彻底读不到。
            body_spill_root=artifact_root / "network-bodies",
        )
        if app_config.traffic.enabled
        else None
    )
    browser_config = replace(
        app_config.browser,
        profile_key=_scoped_profile_key(scope, start_url),
    )
    driver = CdpAutomationDriver(
        browser_config,
        artifact_root,
        network_capture=network_capture,
        network_traffic=traffic_log,
    )
    traffic_inspector = (
        build_traffic_inspector(
            traffic_log,
            driver,
            artifact_root,
            config=app_config.traffic,
            allowed_origins=effective_origins,
        )
        if traffic_log is not None
        else None
    )
    structured_extractor = CdpDomCollectionExtractor(driver, artifact_root)
    memory_runtime = _build_memory_runtime(app_config)
    toolkit = BrowserToolkit(
        driver,
        task,
        visual_context_available=allow_visual_actions,
        structured_extractor=structured_extractor,
        network_data_extractor=network_capture,
        network_traffic_inspector=traffic_inspector,
        memory_runtime=memory_runtime,
        respect_robots=respect_robots,
        min_request_interval_ms=min_request_interval_ms,
        crawl_agent=crawl_agent,
    )
    return toolkit, driver


def build_traffic_inspector(
    log: NetworkTrafficLog,
    driver: CdpAutomationDriver,
    artifact_root: Path,
    *,
    config: NetworkTrafficConfig,
    allowed_origins: Sequence[str],
) -> NetworkTrafficInspector:
    """把流量检查器接到驱动的当前页面会话上，重放才有可用的执行现场。"""

    inspector = NetworkTrafficInspector(
        log,
        artifact_root,
        config=config,
        allowed_origins=allowed_origins,
    )

    def _current_context() -> TrafficSessionContext | None:
        session = driver.session
        if session is None:
            return None
        recorder = driver.network_recorder
        return TrafficSessionContext(
            session=session,
            router=recorder.router if recorder is not None else None,
            page_url=driver.last_known_url,
        )

    inspector.bind_session_source(_current_context)
    return inspector


@asynccontextmanager
async def launch_browser_toolkit(
    start_url: str,
    *,
    goal: str = "外部浏览器工具调用会话",
    config: AppConfig | None = None,
    inputs: Mapping[str, Any] | None = None,
    allowed_origins: Sequence[str] | None = None,
    project_id: str = "toolkit",
    tenant_id: str = "default",
    account_id: str = "default",
    task_id: str | None = None,
    allow_visual_actions: bool = False,
    respect_robots: bool = False,
    min_request_interval_ms: float = 0.0,
    crawl_agent: str = DEFAULT_CRAWL_AGENT,
    read_only: bool | None = None,
) -> AsyncIterator[BrowserToolkit]:
    """启动或接管浏览器、打开入口页面并交出工具会话；退出时关闭浏览器。

    `read_only=True` 会阻止点击、输入、上传、存储写入和请求重放等副作用工具；
    若部署配置已经启用只读，显式传入 False 也不会放宽策略。

    用法::

        async with launch_browser_toolkit("https://example.com/login") as toolkit:
            observation = await toolkit.observe()
            await toolkit.click(observation.candidates[0].target_id,
                                expect_kind="url_contains", expect_value="/home")
    """

    toolkit, driver = build_browser_toolkit(
        start_url,
        goal=goal,
        config=config,
        inputs=inputs,
        allowed_origins=allowed_origins,
        project_id=project_id,
        tenant_id=tenant_id,
        account_id=account_id,
        task_id=task_id,
        allow_visual_actions=allow_visual_actions,
        respect_robots=respect_robots,
        min_request_interval_ms=min_request_interval_ms,
        crawl_agent=crawl_agent,
        read_only=read_only,
    )
    if toolkit.memory_runtime is not None:
        toolkit.memory_runtime.start()
    try:
        await toolkit.open(start_url)
        yield toolkit
    finally:
        if toolkit.memory_runtime is not None:
            await toolkit.memory_runtime.close(timeout_seconds=5.0)
        await driver.close()


def toolkit_usage_reference(*, category: str | None = None) -> dict[str, Any]:
    """输出面向外部智能体的调用参考：分组、契约和会话生命周期约定。"""

    toolkit_categories: dict[str, list[dict[str, Any]]] = {}
    from witty_browser_auto.toolkit.catalog import BROWSER_TOOLS

    for definition in BROWSER_TOOLS.definitions():
        if not definition.externally_callable:
            continue
        if category is not None and definition.category != category:
            continue
        toolkit_categories.setdefault(definition.category, []).append(definition.describe())
    return {
        "entrypoint": "witty_browser_auto.toolkit.launch_browser_toolkit",
        "lifecycle": (
            "async with launch_browser_toolkit(start_url) as toolkit 打开会话；"
            "先 observe() 拿候选 target_id，再调用元素类工具；"
            "页面动作成功后旧观察自动作废，下一次调用会重新观察。"
            "采集优先 try replay_collection_program()，失配再 inspect + run_structured_extraction"
        ),
        "sensitive_inputs": (
            "敏感值放进 inputs 并用 input_key 引用；工具参数、事件与轨迹只保留键名"
        ),
        "read_only": (
            "配置 security.read_only=true 或传 read_only=True 后，副作用工具在触碰浏览器前拒绝；"
            "只读观察、采集、诊断、证据导出仍可用"
        ),
        "categories": toolkit_categories,
    }

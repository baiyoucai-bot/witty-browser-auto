"""公共浏览器工具入口：把已注册工具暴露为可直接 await 的函数。

外部智能体或脚本只需要一个 `AutomationDriver` 和一个 `TaskSpec`，就能按工具名调用
全部开放能力，不需要自行拼装模型工具调用、观察绑定或阶段门控。任务终态类工具由
智能体循环负责，本入口会明确拒绝并说明替代路径。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from witty_browser_auto.agent.crawl_tools import DEFAULT_CRAWL_AGENT
from witty_browser_auto.agent.tools import ToolExecutionResult, ToolExecutor
from witty_browser_auto.domain.models import ModelToolCall, Observation, TaskSpec
from witty_browser_auto.domain.protocols import (
    AutomationDriver,
    NetworkDataExtractor,
    StructuredDataExtractor,
)
from witty_browser_auto.memory.background import BackgroundMemoryRuntime
from witty_browser_auto.network.inspection import NetworkTrafficInspector
from witty_browser_auto.toolkit.catalog import BROWSER_TOOLS
from witty_browser_auto.toolkit.registry import ToolArgumentError, ToolDefinition, ToolRegistry
from witty_browser_auto.toolkit.serialization import observation_to_dict, observation_to_prompt

logger = logging.getLogger(__name__)

# 这些工具读取当前观察中的 target_id，页面变化后必须重新观察才能继续使用。
_OBSERVATION_REFRESH_EXEMPT = frozenset({"wait", "screenshot", "inspect_visual_region"})
# 这些工具不计动作步数，但一旦成功就意味着页面已经变成了另一副样子。
_PAGE_CHANGED_ON_SUCCESS = frozenset({"wait_for_condition"})
# 这些动作的业务后置条件可以省略，缺省按"页面有变化"校验并自动绑定当前观察指纹。
# 等待与拖拽不在其中：等待的条件就是它的全部意义，拖拽必须证明落点而非仅有变化。
_DEFAULT_PAGE_CHANGE_TOOLS = frozenset(
    {
        "click",
        "click_locator",
        "hover",
        "select",
        "select_locator",
        "press_key",
        "navigate_history",
    }
)


class BrowserToolkit:
    """按工具名调用浏览器能力的公共入口。

    与智能体循环共用同一套执行器，因此后置条件校验、脱敏、非幂等防重放和安全挑战
    约束完全一致；区别只是调用方由代码而不是模型决定下一步。
    """

    def __init__(
        self,
        driver: AutomationDriver,
        task: TaskSpec,
        *,
        visual_context_available: bool = False,
        structured_extractor: StructuredDataExtractor | None = None,
        network_data_extractor: NetworkDataExtractor | None = None,
        network_traffic_inspector: NetworkTrafficInspector | None = None,
        memory_runtime: BackgroundMemoryRuntime | None = None,
        respect_robots: bool = False,
        min_request_interval_ms: float = 0.0,
        crawl_agent: str = DEFAULT_CRAWL_AGENT,
        refresh_observation_after_action: bool = True,
    ) -> None:
        self._driver = driver
        self._task = task
        self._memory_runtime = memory_runtime
        # 页面动作收口后立刻重新观察并把新观察挂到结果上：智能体每一步都要看新页面，
        # 与其让它再发一次 observe，不如在同一次返回里给足。关掉后退回惰性观察。
        self._refresh_observation_after_action = refresh_observation_after_action
        self._executor = ToolExecutor(
            driver,
            task,
            visual_context_available=visual_context_available,
            structured_extractor=structured_extractor,
            network_data_extractor=network_data_extractor,
            network_traffic_inspector=network_traffic_inspector,
            memory_runtime=memory_runtime,
            respect_robots=respect_robots,
            min_request_interval_ms=min_request_interval_ms,
            crawl_agent=crawl_agent,
        )
        self._observation: Observation | None = None

    @property
    def driver(self) -> AutomationDriver:
        return self._driver

    @property
    def task(self) -> TaskSpec:
        return self._task

    @property
    def memory_runtime(self) -> BackgroundMemoryRuntime | None:
        return self._memory_runtime

    @property
    def registry(self) -> ToolRegistry:
        return BROWSER_TOOLS

    @property
    def observation(self) -> Observation | None:
        """最近一次页面观察；执行有副作用的动作后会失效。"""

        return self._observation

    def tool_names(self) -> tuple[str, ...]:
        return tuple(definition.name for definition in BROWSER_TOOLS.externally_callable())

    def describe_tools(
        self,
        *,
        category: str | None = None,
        include_engine_only: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        """输出可读工具契约，供外部智能体据此生成调用代码。"""

        definitions = (
            BROWSER_TOOLS.in_category(category) if category else BROWSER_TOOLS.definitions()
        )
        return tuple(
            definition.describe()
            for definition in definitions
            if include_engine_only or definition.externally_callable
        )

    async def observe(self, *, force: bool = False) -> Observation:
        """获取当前页面观察；候选中的 target_id 可直接用于元素类工具。"""

        if force or self._observation is None:
            self._observation = await self._driver.observe(force=force)
        return self._observation

    async def observe_for_model(
        self,
        *,
        force: bool = False,
        as_text: bool = False,
        **options: Any,
    ) -> dict[str, Any] | str:
        """观察当前页面并直接返回可喂给模型的结构。

        `as_text=False` 返回可 `json.dumps` 的紧凑字典，`as_text=True` 返回紧凑文本。
        候选默认按置信度截断到 24 个并标注截断事实；其余预算参数见
        `witty_browser_auto.toolkit.serialization`。
        """

        observation = await self.observe(force=force)
        if as_text:
            return observation_to_prompt(observation, **options)
        return observation_to_dict(observation, **options)

    def invalidate_observation(self) -> None:
        """在外部直接操作页面后手动作废缓存观察。"""

        self._observation = None

    async def open(self, url: str) -> str:
        """启动或复用浏览器表面并打开入口地址，返回表面 ID。"""

        surface_id = await self._driver.open(url)
        self._observation = None
        return surface_id

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        /,
        **overrides: Any,
    ) -> ToolExecutionResult:
        """按工具名执行一次调用；参数在本地校验后才会进入执行器。"""

        definition = BROWSER_TOOLS.get(name)
        if not definition.externally_callable:
            raise ToolArgumentError(
                f"工具 {name} 不开放外部直接调用：{definition.unavailable_reason}"
            )
        merged: dict[str, Any] = dict(arguments or {})
        merged.update(overrides)
        merged = {key: value for key, value in merged.items() if value is not None}
        observation = await self._prepare_observation(definition, merged)
        validated = definition.validate_arguments(merged)
        call = ModelToolCall(
            call_id=f"toolkit-{uuid.uuid4().hex[:16]}",
            name=name,
            arguments=validated,
        )
        result = await self._executor.execute(call, observation)
        if self._page_may_have_changed(name, result):
            self._observation = None
            if self._refresh_observation_after_action:
                result = await self._attach_fresh_observation(result)
        return result

    @staticmethod
    def _page_may_have_changed(name: str, result: ToolExecutionResult) -> bool:
        if name in _PAGE_CHANGED_ON_SUCCESS:
            return result.success
        return result.counts_as_action and name not in _OBSERVATION_REFRESH_EXEMPT

    async def _attach_fresh_observation(self, result: ToolExecutionResult) -> ToolExecutionResult:
        """动作之后重新观察并挂到结果上；观察失败不能掩盖动作本身的结论。"""

        try:
            self._observation = await self._driver.observe(force=True)
        except Exception:
            logger.warning(
                "动作后重新观察失败，结果不附带页面快照",
                extra={"tool": result.name},
                exc_info=True,
            )
            self._observation = None
            return result
        return replace(result, observation=self._observation)

    async def _prepare_observation(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
    ) -> Observation:
        observation = await self.observe()
        if definition.requires_observation and "observation_fingerprint" in definition.properties:
            # 观察指纹属于执行期状态，由代码绑定比让调用方手抄更可靠。
            arguments.setdefault("observation_fingerprint", observation.fingerprint)
        if (
            definition.name in _DEFAULT_PAGE_CHANGE_TOOLS
            and "expect_kind" not in arguments
            and "expect_value" not in arguments
        ):
            # 调用方没声明业务后置条件时退到"页面有变化"：点开菜单、切换标签、展开详情
            # 这类探索性动作事先说不清结果，逼调用方编一个判据只会换来一次被拒绝的回合。
            # 业务判据（url/text）仍然更强，知道结果时应当优先给。
            arguments["expect_kind"] = "fingerprint_changed"
        if (
            arguments.get("expect_kind") == "fingerprint_changed"
            and not arguments.get("expect_value")
            and "expect_value" in definition.properties
        ):
            # 页面变化条件必须绑定当前观察指纹，调用方没有理由手抄这个执行期值。
            arguments["expect_value"] = observation.fingerprint
        return observation

    # ---- 导航与页面 ----

    async def navigate(self, url: str) -> ToolExecutionResult:
        return await self.call("navigate", url=url)

    async def scroll(self, amount: float) -> ToolExecutionResult:
        return await self.call("scroll", amount=amount)

    async def wait(self, seconds: float) -> ToolExecutionResult:
        return await self.call("wait", seconds=seconds)

    async def screenshot(self, label: str) -> ToolExecutionResult:
        return await self.call("screenshot", label=label)

    async def check_crawl_policy(
        self,
        *,
        url: str | None = None,
        agent: str | None = None,
        refresh: bool | None = None,
    ) -> ToolExecutionResult:
        """读取 robots.txt 并判定是否允许抓取；`data["allowed"]` 为 None 表示状态未知。"""

        return await self.call("check_crawl_policy", url=url, agent=agent, refresh=refresh)

    async def read_page_markdown(
        self,
        *,
        only_main_content: bool | None = None,
        selector: str | None = None,
        include_links: bool | None = None,
        include_images: bool | None = None,
        max_chars: int | None = None,
    ) -> ToolExecutionResult:
        """把当前页面主内容转成 Markdown；`data["markdown"]` 可直接进模型上下文。"""

        return await self.call(
            "read_page_markdown",
            only_main_content=only_main_content,
            selector=selector,
            include_links=include_links,
            include_images=include_images,
            max_chars=max_chars,
        )

    async def list_page_links(
        self,
        *,
        same_origin_only: bool | None = None,
        contains: str | None = None,
        include_images: bool | None = None,
        limit: int | None = None,
    ) -> ToolExecutionResult:
        """列出页面链接；`data["links"]` 每项含绝对地址、文本与是否同源。"""

        return await self.call(
            "list_page_links",
            same_origin_only=same_origin_only,
            contains=contains,
            include_images=include_images,
            limit=limit,
        )

    async def capture_annotated_screenshot(
        self,
        *,
        label: str | None = None,
        max_labels: int | None = None,
        roles: Sequence[str] | None = None,
    ) -> ToolExecutionResult:
        """截图并叠加候选编号；`data["legend"]` 给出编号到 target_id 的对应关系。"""

        return await self.call(
            "capture_annotated_screenshot",
            label=label,
            max_labels=max_labels,
            roles=list(roles) if roles is not None else None,
        )

    async def navigate_history(
        self,
        action: str,
        *,
        expect_kind: str = "fingerprint_changed",
        expect_value: str | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "navigate_history",
            action=action,
            expect_kind=expect_kind,
            expect_value=expect_value,
        )

    async def go_back(
        self,
        *,
        expect_kind: str = "fingerprint_changed",
        expect_value: str | None = None,
    ) -> ToolExecutionResult:
        return await self.navigate_history(
            "back", expect_kind=expect_kind, expect_value=expect_value
        )

    async def go_forward(
        self,
        *,
        expect_kind: str = "fingerprint_changed",
        expect_value: str | None = None,
    ) -> ToolExecutionResult:
        return await self.navigate_history(
            "forward", expect_kind=expect_kind, expect_value=expect_value
        )

    async def reload(
        self,
        *,
        expect_kind: str = "fingerprint_changed",
        expect_value: str | None = None,
    ) -> ToolExecutionResult:
        return await self.navigate_history(
            "reload", expect_kind=expect_kind, expect_value=expect_value
        )

    # ---- 语义候选元素动作 ----

    async def click(
        self,
        target_id: str,
        *,
        expect_kind: str | None = None,
        expect_value: str | None = None,
        button: str | None = None,
        click_count: int | None = None,
    ) -> ToolExecutionResult:
        """点击候选；不给后置条件时按"页面有变化"校验（自动绑定当前观察指纹）。"""

        return await self.call(
            "click",
            target_id=target_id,
            expect_kind=expect_kind,
            expect_value=expect_value,
            button=button,
            click_count=click_count,
        )

    async def right_click(
        self,
        target_id: str | None = None,
        *,
        expect_kind: str | None = None,
        expect_value: str | None = None,
        locator: Mapping[str, Any] | None = None,
    ) -> ToolExecutionResult:
        return await self._pointer_click(
            target_id,
            locator=locator,
            expect_kind=expect_kind,
            expect_value=expect_value,
            button="right",
        )

    async def double_click(
        self,
        target_id: str | None = None,
        *,
        expect_kind: str | None = None,
        expect_value: str | None = None,
        locator: Mapping[str, Any] | None = None,
    ) -> ToolExecutionResult:
        return await self._pointer_click(
            target_id,
            locator=locator,
            expect_kind=expect_kind,
            expect_value=expect_value,
            click_count=2,
        )

    async def _pointer_click(
        self,
        target_id: str | None,
        *,
        locator: Mapping[str, Any] | None,
        expect_kind: str | None,
        expect_value: str | None,
        button: str | None = None,
        click_count: int | None = None,
    ) -> ToolExecutionResult:
        """右键与双击的目标常是行、卡片这类没有语义角色的元素。

        这类元素不会出现在观察候选里，只能靠定位器指过去，所以两个便捷方法都要
        同时接受 target_id 和 locator。
        """

        if locator is None:
            if target_id is None:
                raise ValueError("需要提供 target_id 或 locator 之一")
            return await self.click(
                target_id,
                expect_kind=expect_kind,
                expect_value=expect_value,
                button=button,
                click_count=click_count,
            )
        if target_id is not None:
            raise ValueError("target_id 与 locator 只能提供一个")
        return await self.click_locator(
            locator,
            expect_kind=expect_kind,
            expect_value=expect_value,
            button=button,
            click_count=click_count,
        )

    async def hover(
        self,
        target_id: str | None = None,
        *,
        expect_kind: str | None = None,
        expect_value: str | None = None,
        locator: Mapping[str, Any] | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "hover",
            target_id=target_id,
            locator=dict(locator) if locator is not None else None,
            expect_kind=expect_kind,
            expect_value=expect_value,
        )

    async def input_text(
        self,
        target_id: str,
        *,
        input_key: str | None = None,
        text: str | None = None,
    ) -> ToolExecutionResult:
        """输入文本：敏感值走 `input_key` 引用任务输入，非敏感字面量直接给 `text`。"""

        return await self.call("input_text", target_id=target_id, input_key=input_key, text=text)

    async def input_generated_text(
        self,
        target_id: str,
        *,
        text: str,
        screenshot_fingerprint: str,
        visual_confidence: float,
        security_challenge: bool,
    ) -> ToolExecutionResult:
        return await self.call(
            "input_generated_text",
            target_id=target_id,
            text=text,
            screenshot_fingerprint=screenshot_fingerprint,
            visual_confidence=visual_confidence,
            security_challenge=security_challenge,
        )

    async def select(
        self,
        target_id: str,
        *,
        expect_kind: str | None = None,
        expect_value: str | None = None,
        value: str | None = None,
        input_key: str | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "select",
            target_id=target_id,
            expect_kind=expect_kind,
            expect_value=expect_value,
            value=value,
            input_key=input_key,
        )

    async def drag(
        self,
        target_id: str,
        *,
        end_dx: float,
        end_dy: float,
        duration_ms: int,
        steps: int,
        security_challenge: bool,
        expect_kind: str,
        expect_value: str,
    ) -> ToolExecutionResult:
        return await self.call(
            "drag",
            target_id=target_id,
            end_dx=end_dx,
            end_dy=end_dy,
            duration_ms=duration_ms,
            steps=steps,
            security_challenge=security_challenge,
            expect_kind=expect_kind,
            expect_value=expect_value,
        )

    async def press_key(
        self,
        key: str,
        *,
        expect_kind: str = "fingerprint_changed",
        expect_value: str | None = None,
        modifiers: Sequence[str] | None = None,
        repeat: int | None = None,
        target_id: str | None = None,
        locator: Mapping[str, Any] | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "press_key",
            key=key,
            expect_kind=expect_kind,
            expect_value=expect_value,
            modifiers=list(modifiers) if modifiers is not None else None,
            repeat=repeat,
            target_id=target_id,
            locator=dict(locator) if locator is not None else None,
        )

    # ---- 元素只读读取 ----

    async def read_element(
        self,
        target_id: str | None = None,
        *,
        locator: Mapping[str, Any] | None = None,
        max_text_length: int | None = None,
        include_html: bool | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "read_element",
            target_id=target_id,
            locator=dict(locator) if locator is not None else None,
            max_text_length=max_text_length,
            include_html=include_html,
        )

    async def capture_element_screenshot(
        self,
        target_id: str | None = None,
        *,
        locator: Mapping[str, Any] | None = None,
        label: str | None = None,
        padding: float | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "capture_element_screenshot",
            target_id=target_id,
            locator=dict(locator) if locator is not None else None,
            label=label,
            padding=padding,
        )

    # ---- iframe 帧 ----

    async def list_frames(self) -> ToolExecutionResult:
        """列出主框架与全部 iframe；返回的 frame_id 可直接放进定位器。"""

        return await self.call("list_frames")

    # ---- 流量检查与重放 ----

    async def inspect_network_traffic(
        self,
        *,
        url_contains: str | None = None,
        methods: Sequence[str] | None = None,
        resource_types: Sequence[str] | None = None,
        status_min: int | None = None,
        status_max: int | None = None,
        only_failed: bool | None = None,
        limit: int | None = None,
    ) -> ToolExecutionResult:
        """列出已发生的网络交换；`data["exchanges"]` 含完整 URL、请求头与响应头。"""

        return await self.call(
            "inspect_network_traffic",
            url_contains=url_contains,
            methods=list(methods) if methods is not None else None,
            resource_types=list(resource_types) if resource_types is not None else None,
            status_min=status_min,
            status_max=status_max,
            only_failed=only_failed,
            limit=limit,
        )

    async def read_network_body(
        self,
        exchange_id: str,
        *,
        part: str | None = None,
    ) -> ToolExecutionResult:
        """读取某次交换的请求体或响应体；JSON 正文额外解析到 `data["json"]`。"""

        return await self.call("read_network_body", exchange_id=exchange_id, part=part)

    async def search_network_traffic(
        self,
        query: str,
        *,
        scope: str | None = None,
        case_sensitive: bool | None = None,
        url_contains: str | None = None,
        resource_types: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> ToolExecutionResult:
        """在已抓取的正文/头/帧/SSE 里搜子串，`data["matches"]` 给出命中交换与片段。"""

        return await self.call(
            "search_network_traffic",
            query=query,
            scope=scope,
            case_sensitive=case_sensitive,
            url_contains=url_contains,
            resource_types=list(resource_types) if resource_types is not None else None,
            limit=limit,
        )

    async def drag_to_element(
        self,
        *,
        source_target_id: str | None = None,
        source_locator: Mapping[str, Any] | None = None,
        target_target_id: str | None = None,
        target_locator: Mapping[str, Any] | None = None,
        steps: int | None = None,
        step_delay_ms: int | None = None,
    ) -> ToolExecutionResult:
        """把源元素拖到目标元素上；`data["channel"]` 说明走的是原生还是鼠标通道。"""

        return await self.call(
            "drag_to_element",
            source_target_id=source_target_id,
            source_locator=dict(source_locator) if source_locator else None,
            target_target_id=target_target_id,
            target_locator=dict(target_locator) if target_locator else None,
            steps=steps,
            step_delay_ms=step_delay_ms,
        )

    async def save_pdf(
        self,
        *,
        label: str | None = None,
        paper: str | None = None,
        landscape: bool | None = None,
        print_background: bool | None = None,
        scale: float | None = None,
        margin_inches: float | None = None,
        page_ranges: str | None = None,
        prefer_css_page_size: bool | None = None,
    ) -> ToolExecutionResult:
        """把当前页面导出为 PDF；`data["pdf_path"]` 是写好的私有文件。"""

        return await self.call(
            "save_pdf",
            label=label,
            paper=paper,
            landscape=landscape,
            print_background=print_background,
            scale=scale,
            margin_inches=margin_inches,
            page_ranges=page_ranges,
            prefer_css_page_size=prefer_css_page_size,
        )

    async def measure_performance(
        self,
        *,
        reload: bool | None = None,
        settle_seconds: float | None = None,
    ) -> ToolExecutionResult:
        """采集 Core Web Vitals 与导航计时；要测 LCP 必须传 `reload=True`。"""

        return await self.call("measure_performance", reload=reload, settle_seconds=settle_seconds)

    async def fill_form(self, fields: Sequence[Mapping[str, Any]]) -> ToolExecutionResult:
        """一次写入多个表单字段；每个字段按自己的真实值回读校验。"""

        return await self.call("fill_form", fields=[dict(field) for field in fields])

    async def wait_for_condition(
        self,
        expect_kind: str,
        expect_value: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolExecutionResult:
        """等待页面条件成立；轮询由代码完成，条件满足即刻返回。"""

        return await self.call(
            "wait_for_condition",
            expect_kind=expect_kind,
            expect_value=expect_value,
            timeout_seconds=timeout_seconds,
        )

    async def manage_storage_state(
        self,
        operation: str,
        *,
        file_path: str | None = None,
        state: Mapping[str, Any] | None = None,
        clear_existing: bool | None = None,
    ) -> ToolExecutionResult:
        """整体导出或导入登录态；导出结果在 `data["state"]` 与 `data["file_path"]`。"""

        return await self.call(
            "manage_storage_state",
            operation=operation,
            file_path=file_path,
            state=dict(state) if state else None,
            clear_existing=clear_existing,
        )

    async def emulate_environment(
        self,
        *,
        device: str | None = None,
        viewport: Mapping[str, Any] | None = None,
        network_preset: str | None = None,
        network: Mapping[str, Any] | None = None,
        cpu_throttle_rate: float | None = None,
        locale: str | None = None,
        timezone: str | None = None,
        color_scheme: str | None = None,
        geolocation: Mapping[str, Any] | None = None,
        reset: bool | None = None,
    ) -> ToolExecutionResult:
        """模拟设备、网络与环境；`data["effective"]` 是页面实际生效的值。"""

        return await self.call(
            "emulate_environment",
            device=device,
            viewport=dict(viewport) if viewport else None,
            network_preset=network_preset,
            network=dict(network) if network else None,
            cpu_throttle_rate=cpu_throttle_rate,
            locale=locale,
            timezone=timezone,
            color_scheme=color_scheme,
            geolocation=dict(geolocation) if geolocation else None,
            reset=reset,
        )

    async def handle_dialog(
        self,
        action: str,
        *,
        scope: str | None = None,
        dialog_kinds: Sequence[str] | None = None,
        prompt_text: str | None = None,
        prompt_text_input_key: str | None = None,
    ) -> ToolExecutionResult:
        """设置弹窗应答方式并查看已接管记录；`action="inspect"` 表示只读查看。"""

        return await self.call(
            "handle_dialog",
            action=action,
            scope=scope,
            dialog_kinds=list(dialog_kinds) if dialog_kinds else None,
            prompt_text=prompt_text,
            prompt_text_input_key=prompt_text_input_key,
        )

    async def export_action_script(self, *, target: str | None = None) -> ToolExecutionResult:
        """把本次会话已验证的页面动作导出成可重跑脚本；`data["code"]` 即脚本文本。"""

        return await self.call("export_action_script", target=target)

    async def read_websocket_frames(
        self,
        exchange_id: str,
        *,
        direction: str | None = None,
        contains: str | None = None,
        limit: int | None = None,
    ) -> ToolExecutionResult:
        """读取 WebSocket 连接的帧内容；`data["frames"]` 每帧含 payload。"""

        return await self.call(
            "read_websocket_frames",
            exchange_id=exchange_id,
            direction=direction,
            contains=contains,
            limit=limit,
        )

    async def read_sse_messages(
        self,
        exchange_id: str,
        *,
        event_name: str | None = None,
        contains: str | None = None,
        limit: int | None = None,
    ) -> ToolExecutionResult:
        """读取 SSE 连接的消息；`data["messages"]` 每条含 data 与解析后的 json。"""

        return await self.call(
            "read_sse_messages",
            exchange_id=exchange_id,
            event_name=event_name,
            contains=contains,
            limit=limit,
        )

    async def analyze_api_endpoint(
        self,
        *,
        exchange_id: str | None = None,
        url_contains: str | None = None,
    ) -> ToolExecutionResult:
        """归纳接口契约：URL 模板、参数表、凭据位置、请求/响应结构与分页策略。"""

        return await self.call(
            "analyze_api_endpoint",
            exchange_id=exchange_id,
            url_contains=url_contains,
        )

    async def collect_api_pages(
        self,
        *,
        exchange_id: str | None = None,
        url_contains: str | None = None,
        strategy: str | None = None,
        page_param: str | None = None,
        page_in: str | None = None,
        cursor_in: str | None = None,
        cursor_header: str | None = None,
        start: int | None = None,
        step: int | None = None,
        page_size: int | None = None,
        record_path: Sequence[str] | None = None,
        total_path: Sequence[str] | None = None,
        cursor_field: str | None = None,
        dedupe_key: str | None = None,
        max_pages: int | None = None,
        delay_ms: int | None = None,
    ) -> ToolExecutionResult:
        """沿分页取全接口数据；`data["records"]` 是全部记录，`data["closed"]` 是闭合结论。

        未闭合时 `success` 为 False，`data["reason"]` 说明缺口在哪。分页字段在 POST 请求体
        里时传 `page_in="body"`；游标在响应头里时传 `cursor_in="header"` 加 `cursor_header`，
        服务端给 `Link: rel=next` 时传 `cursor_in="link"`。
        """

        return await self.call(
            "collect_api_pages",
            exchange_id=exchange_id,
            url_contains=url_contains,
            strategy=strategy,
            page_param=page_param,
            page_in=page_in,
            cursor_in=cursor_in,
            cursor_header=cursor_header,
            start=start,
            step=step,
            page_size=page_size,
            record_path=list(record_path) if record_path is not None else None,
            total_path=list(total_path) if total_path is not None else None,
            cursor_field=cursor_field,
            dedupe_key=dedupe_key,
            max_pages=max_pages,
            delay_ms=delay_ms,
        )

    async def export_request_code(
        self,
        exchange_id: str,
        *,
        target: str | None = None,
        include_secrets: bool | None = None,
    ) -> ToolExecutionResult:
        """把捕获的请求导出成可独立运行的代码；`data["code"]` 即代码文本。"""

        return await self.call(
            "export_request_code",
            exchange_id=exchange_id,
            target=target,
            include_secrets=include_secrets,
        )

    async def export_network_har(
        self,
        collection_name: str,
        *,
        url_contains: str | None = None,
        methods: Sequence[str] | None = None,
        resource_types: Sequence[str] | None = None,
        status_min: int | None = None,
        status_max: int | None = None,
        only_failed: bool | None = None,
        include_bodies: bool | None = None,
        limit: int | None = None,
    ) -> ToolExecutionResult:
        """导出 HAR 1.2 私有文件，可直接导入 Reqable 或 Charles 二次分析。"""

        return await self.call(
            "export_network_har",
            collection_name=collection_name,
            url_contains=url_contains,
            methods=list(methods) if methods is not None else None,
            resource_types=list(resource_types) if resource_types is not None else None,
            status_min=status_min,
            status_max=status_max,
            only_failed=only_failed,
            include_bodies=include_bodies,
            limit=limit,
        )

    async def replay_network_request(
        self,
        *,
        exchange_id: str | None = None,
        url: str | None = None,
        method: str | None = None,
        headers: Mapping[str, str] | None = None,
        remove_headers: Sequence[str] | None = None,
        body: str | None = None,
        referrer: str | None = None,
    ) -> ToolExecutionResult:
        """重放或编辑重发：只给 exchange_id 即原样重放，再给其他字段即逐项覆盖。"""

        return await self.call(
            "replay_network_request",
            exchange_id=exchange_id,
            url=url,
            method=method,
            headers=dict(headers) if headers is not None else None,
            remove_headers=list(remove_headers) if remove_headers is not None else None,
            body=body,
            referrer=referrer,
        )

    # ---- 显式定位器动作 ----

    async def click_locator(
        self,
        locator: Mapping[str, Any],
        *,
        expect_kind: str | None = None,
        expect_value: str | None = None,
        button: str | None = None,
        click_count: int | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "click_locator",
            locator=dict(locator),
            expect_kind=expect_kind,
            expect_value=expect_value,
            button=button,
            click_count=click_count,
        )

    async def input_text_locator(
        self,
        locator: Mapping[str, Any],
        *,
        input_key: str | None = None,
        text: str | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "input_text_locator", locator=dict(locator), input_key=input_key, text=text
        )

    async def select_locator(
        self,
        locator: Mapping[str, Any],
        *,
        expect_kind: str | None = None,
        expect_value: str | None = None,
        value: str | None = None,
        input_key: str | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "select_locator",
            locator=dict(locator),
            expect_kind=expect_kind,
            expect_value=expect_value,
            value=value,
            input_key=input_key,
        )

    # ---- 视觉动作 ----

    async def visual_click(
        self,
        *,
        screenshot_fingerprint: str,
        x_ratio: float,
        y_ratio: float,
        visual_confidence: float,
        expect_kind: str,
        expect_value: str,
    ) -> ToolExecutionResult:
        return await self.call(
            "visual_click",
            screenshot_fingerprint=screenshot_fingerprint,
            x_ratio=x_ratio,
            y_ratio=y_ratio,
            visual_confidence=visual_confidence,
            expect_kind=expect_kind,
            expect_value=expect_value,
        )

    async def visual_drag(
        self,
        *,
        screenshot_fingerprint: str,
        start_x_ratio: float,
        start_y_ratio: float,
        end_x_ratio: float,
        end_y_ratio: float,
        duration_ms: int,
        steps: int,
        visual_confidence: float,
        security_challenge: bool,
        expect_kind: str,
        expect_value: str,
        motion_profile: str | None = None,
        geometry_mode: str | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "visual_drag",
            screenshot_fingerprint=screenshot_fingerprint,
            start_x_ratio=start_x_ratio,
            start_y_ratio=start_y_ratio,
            end_x_ratio=end_x_ratio,
            end_y_ratio=end_y_ratio,
            duration_ms=duration_ms,
            steps=steps,
            visual_confidence=visual_confidence,
            security_challenge=security_challenge,
            expect_kind=expect_kind,
            expect_value=expect_value,
            motion_profile=motion_profile,
            geometry_mode=geometry_mode,
        )

    async def inspect_visual_region(
        self,
        *,
        screenshot_fingerprint: str,
        x_ratio: float,
        y_ratio: float,
        width_ratio: float,
        height_ratio: float,
        visual_confidence: float,
    ) -> ToolExecutionResult:
        return await self.call(
            "inspect_visual_region",
            screenshot_fingerprint=screenshot_fingerprint,
            x_ratio=x_ratio,
            y_ratio=y_ratio,
            width_ratio=width_ratio,
            height_ratio=height_ratio,
            visual_confidence=visual_confidence,
        )

    # ---- 结构化采集 ----

    async def inspect_collection_structure(self) -> ToolExecutionResult:
        return await self.call("inspect_collection_structure")

    async def run_structured_extraction(
        self,
        *,
        collection_name: str,
        candidate_id: str,
        unique_field_id: str | None = None,
        detail_field_id: str | None = None,
        filters: Sequence[Mapping[str, Any]] | None = None,
        max_pages: int | None = None,
        max_items: int | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "run_structured_extraction",
            collection_name=collection_name,
            candidate_id=candidate_id,
            unique_field_id=unique_field_id,
            detail_field_id=detail_field_id,
            filters=[dict(item) for item in filters] if filters is not None else None,
            max_pages=max_pages,
            max_items=max_items,
        )

    async def replay_collection_program(self) -> ToolExecutionResult:
        """命中已验证采集程序时零模型重放；失配则返回失败并提示回退检查结构。"""

        return await self.call("replay_collection_program")

    # ---- 网络数据 ----

    async def inspect_network_data(
        self,
        *,
        max_candidates: int | None = None,
    ) -> ToolExecutionResult:
        return await self.call("inspect_network_data", max_candidates=max_candidates)

    async def export_network_response(
        self,
        *,
        collection_name: str,
        candidate_id: str | None = None,
        candidate_ids: Sequence[str] | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "export_network_response",
            collection_name=collection_name,
            candidate_id=candidate_id,
            candidate_ids=list(candidate_ids) if candidate_ids is not None else None,
        )

    async def wait_network_response(
        self,
        url_substring: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "wait_network_response",
            url_substring=url_substring,
            timeout_seconds=timeout_seconds,
        )

    async def manage_network_route(
        self,
        operation: str,
        **config: Any,
    ) -> ToolExecutionResult:
        return await self.call("manage_network_route", operation=operation, **config)

    # ---- 标签页与诊断 ----

    async def list_tabs(self) -> ToolExecutionResult:
        return await self.call("list_tabs")

    async def open_tab(self, url: str) -> ToolExecutionResult:
        return await self.call("open_tab", url=url)

    async def switch_tab(self, target_id: str) -> ToolExecutionResult:
        return await self.call("switch_tab", target_id=target_id)

    async def close_tab(self, target_id: str) -> ToolExecutionResult:
        return await self.call("close_tab", target_id=target_id)

    # ---- 文件上传与下载 ----

    async def upload_files(
        self,
        target_id: str | None = None,
        *,
        paths: Sequence[str] | None = None,
        path_input_keys: Sequence[str] | None = None,
        locator: Mapping[str, Any] | None = None,
        expect_kind: str | None = None,
        expect_value: str | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "upload_files",
            target_id=target_id,
            paths=list(paths) if paths is not None else None,
            path_input_keys=list(path_input_keys) if path_input_keys is not None else None,
            locator=dict(locator) if locator is not None else None,
            expect_kind=expect_kind,
            expect_value=expect_value,
        )

    async def list_downloads(self, *, limit: int | None = None) -> ToolExecutionResult:
        return await self.call("list_downloads", limit=limit)

    async def wait_for_download(
        self,
        *,
        suggested_filename: str | None = None,
        url_contains: str | None = None,
        timeout_seconds: float | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "wait_for_download",
            suggested_filename=suggested_filename,
            url_contains=url_contains,
            timeout_seconds=timeout_seconds,
        )

    # ---- Cookie 与 Web Storage ----

    async def read_cookies(
        self,
        *,
        url: str | None = None,
        names: Sequence[str] | None = None,
    ) -> ToolExecutionResult:
        return await self.call("read_cookies", url=url, names=list(names) if names else None)

    async def set_cookie(
        self,
        name: str,
        *,
        value: str | None = None,
        value_input_key: str | None = None,
        url: str | None = None,
        path: str | None = None,
        domain: str | None = None,
        http_only: bool | None = None,
        secure: bool | None = None,
        expires: float | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "set_cookie",
            name=name,
            value=value,
            value_input_key=value_input_key,
            url=url,
            path=path,
            domain=domain,
            http_only=http_only,
            secure=secure,
            expires=expires,
        )

    async def read_web_storage(
        self,
        storage_kind: str,
        *,
        key: str | None = None,
        frame_id: str | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "read_web_storage",
            storage_kind=storage_kind,
            key=key,
            frame_id=frame_id,
        )

    async def write_web_storage(
        self,
        storage_kind: str,
        key: str,
        *,
        value: str | None = None,
        value_input_key: str | None = None,
        frame_id: str | None = None,
        remove: bool | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "write_web_storage",
            storage_kind=storage_kind,
            key=key,
            value=value,
            value_input_key=value_input_key,
            frame_id=frame_id,
            remove=remove,
        )

    async def inspect_page_diagnostics(
        self,
        *,
        max_console: int | None = None,
        max_network: int | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "inspect_page_diagnostics",
            max_console=max_console,
            max_network=max_network,
        )

    async def report_capability_gap(
        self,
        *,
        area: str,
        capability: str,
        evidence: str,
        related_tool: str | None = None,
    ) -> ToolExecutionResult:
        return await self.call(
            "report_capability_gap",
            area=area,
            capability=capability,
            evidence=evidence,
            related_tool=related_tool,
        )

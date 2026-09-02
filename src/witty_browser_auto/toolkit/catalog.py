"""全部浏览器工具的声明目录。

这里是工具名称、参数、返回约定和调用限制的唯一来源。模型 schema、执行层分发和
外部调用方都从本目录派生；新增能力只需要在这里追加一条声明。
"""

from __future__ import annotations

from typing import Any

from witty_browser_auto.agent.crawl_tools import DEFAULT_CRAWL_AGENT
from witty_browser_auto.browser.annotation import DEFAULT_MAX_LABELS as DEFAULT_ANNOTATION_LABELS
from witty_browser_auto.browser.annotation import MAX_LABELS as MAX_ANNOTATION_LABELS
from witty_browser_auto.browser.dialogs import DIALOG_KINDS
from witty_browser_auto.browser.emulation import COLOR_SCHEMES, DEVICE_PRESETS, NETWORK_PRESETS
from witty_browser_auto.browser.form_fill import (
    MAX_FIELDS as MAX_FORM_FIELDS,
)
from witty_browser_auto.browser.form_fill import (
    MAX_TEXT_LENGTH as MAX_FORM_TEXT_LENGTH,
)
from witty_browser_auto.browser.keyboard import supported_key_names, supported_modifier_names
from witty_browser_auto.browser.mouse import MAX_CLICK_COUNT, POINTER_BUTTONS
from witty_browser_auto.browser.page_content import DEFAULT_MAX_CHARS as DEFAULT_PAGE_MARKDOWN_CHARS
from witty_browser_auto.browser.page_content import DEFAULT_MAX_LINKS as DEFAULT_PAGE_LINKS
from witty_browser_auto.browser.page_content import MAX_CHARS as MAX_PAGE_MARKDOWN_CHARS
from witty_browser_auto.browser.page_content import MAX_LINKS as MAX_PAGE_LINKS
from witty_browser_auto.browser.page_export import PAPER_SIZES
from witty_browser_auto.network.codegen import CODE_TARGETS
from witty_browser_auto.network.pagination import CURSOR_SOURCES, PAGE_LOCATIONS
from witty_browser_auto.network.pagination import DEFAULT_MAX_PAGES as DEFAULT_PAGINATION_PAGES
from witty_browser_auto.network.pagination import MAX_PAGES as MAX_PAGINATION_PAGES
from witty_browser_auto.network.pagination import STRATEGIES as PAGINATION_STRATEGIES
from witty_browser_auto.toolkit.registry import ToolDefinition, ToolRegistry
from witty_browser_auto.toolkit.script_export import SCRIPT_TARGETS

# 键名与修饰键契约直接来自键盘模块，避免 schema 和实现各写一份。
SUPPORTED_KEY_NAMES: tuple[str, ...] = supported_key_names()
SUPPORTED_MODIFIER_NAMES: tuple[str, ...] = supported_modifier_names()

CAPABILITY_AREAS: tuple[str, ...] = (
    "browser_action",
    "locator",
    "network_data",
    "output_delivery",
    "recovery",
    "structured_extraction",
)

LOCATOR_PROPERTY: dict[str, Any] = {
    "type": "object",
    "description": "显式定位器；普通情况下优先使用观察结果中的 target_id",
    "properties": {
        "strategy": {
            "type": "string",
            "enum": ["css", "xpath", "role", "text", "label", "test_id"],
        },
        "value": {"type": "string", "minLength": 1, "maxLength": 1024},
        "name": {
            "type": "string",
            "maxLength": 300,
            "description": "role 策略可用的可访问名称",
        },
        "exact": {"type": "boolean"},
        "index": {"type": "integer", "minimum": 0, "maximum": 100},
        "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 15},
        "frame_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": (
                "在指定 iframe 内定位，取值来自 list_frames；省略表示只在主框架内查找。"
                "主框架的定位器不会穿透任何 iframe"
            ),
        },
    },
    "required": ["strategy", "value"],
    "additionalProperties": False,
}

POINTER_BUTTON_PROPERTY: dict[str, Any] = {
    "type": "string",
    "enum": list(POINTER_BUTTONS),
    "description": "鼠标按键，默认 left；right 会触发页面的 contextmenu",
}

CLICK_COUNT_PROPERTY: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_CLICK_COUNT,
    "description": "连续点击次数，默认 1；2 表示双击",
}

_ENGINE_ONLY = "该工具由智能体循环处理任务终态与等待，外部调用方应直接读取工具返回值自行决策"

CORE_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="navigate",
        description="导航到任务授权范围内的网页",
        category="navigation",
        properties={"url": {"type": "string"}},
        required=("url",),
        returns="导航回执与后置校验结果；越权 URL 会被导航策略拒绝",
        idempotent=False,
    ),
    ToolDefinition(
        name="visual_click",
        description=(
            "仅在多模态截图能看见目标但语义候选中没有 target_id 时使用；"
            "按当前视口比例坐标点击，并绑定当前观察、截图和视觉置信度"
        ),
        category="visual",
        properties={
            "observation_fingerprint": {"type": "string"},
            "screenshot_fingerprint": {"type": "string"},
            "x_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "y_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "visual_confidence": {"type": "number", "minimum": 0.8, "maximum": 1},
            "expect_kind": {
                "type": "string",
                "enum": ["url_contains", "title_contains", "text_contains"],
            },
            "expect_value": {"type": "string"},
        },
        required=(
            "observation_fingerprint",
            "screenshot_fingerprint",
            "x_ratio",
            "y_ratio",
            "visual_confidence",
            "expect_kind",
            "expect_value",
        ),
        returns="点击回执与业务后置条件校验结果",
        requires_observation=True,
        idempotent=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="inspect_visual_region",
        description=(
            "当验证码、图标或局部文字在整页截图中过小时，按当前视口比例裁剪并放大；"
            "这是只读观察，放大图只在下一轮模型决策中临时提供"
        ),
        category="visual",
        properties={
            "observation_fingerprint": {"type": "string"},
            "screenshot_fingerprint": {"type": "string"},
            "x_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "y_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "width_ratio": {"type": "number", "minimum": 0.05, "maximum": 1},
            "height_ratio": {"type": "number", "minimum": 0.05, "maximum": 1},
            "visual_confidence": {"type": "number", "minimum": 0.8, "maximum": 1},
        },
        required=(
            "observation_fingerprint",
            "screenshot_fingerprint",
            "x_ratio",
            "y_ratio",
            "width_ratio",
            "height_ratio",
            "visual_confidence",
        ),
        returns="放大区域截图证据引用；不改变页面状态",
        requires_observation=True,
        counts_as_action=False,
    ),
    ToolDefinition(
        name="visual_drag",
        description=(
            "仅在多模态截图中没有语义目标时使用；按当前视口比例坐标拖拽，"
            "必须绑定当前观察指纹和视觉置信度。安全挑战失败后必须切换"
            "motion_profile 或 geometry_mode，重复策略会在执行前被拒绝且不消耗次数"
        ),
        category="visual",
        properties={
            "observation_fingerprint": {"type": "string"},
            "screenshot_fingerprint": {"type": "string"},
            "start_x_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "start_y_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "end_x_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "end_y_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "duration_ms": {"type": "integer", "minimum": 100, "maximum": 5000},
            "steps": {"type": "integer", "minimum": 2, "maximum": 120},
            "motion_profile": {
                "type": "string",
                "enum": ["balanced", "steady", "ease_out", "hesitant"],
                "description": "运动策略；失败重试必须选择尚未失败的策略",
            },
            "geometry_mode": {
                "type": "string",
                "enum": ["track", "model"],
                "description": ("track 使用代码轨道几何；model 使用当前截图坐标并接受轨道边界校验"),
            },
            "visual_confidence": {"type": "number", "minimum": 0.8, "maximum": 1},
            "security_challenge": {"type": "boolean"},
            "expect_kind": {
                "type": "string",
                "enum": ["url_contains", "title_contains", "text_contains"],
            },
            "expect_value": {"type": "string"},
        },
        required=(
            "observation_fingerprint",
            "screenshot_fingerprint",
            "start_x_ratio",
            "start_y_ratio",
            "end_x_ratio",
            "end_y_ratio",
            "duration_ms",
            "steps",
            "visual_confidence",
            "security_challenge",
            "expect_kind",
            "expect_value",
        ),
        returns="拖拽回执、轨迹审计摘要与业务后置条件校验结果",
        requires_observation=True,
        idempotent=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="drag",
        description=(
            "从当前目标中心按平滑轨迹拖拽；普通业务滑块可直接使用，安全挑战必须显式标记且任务已授权"
        ),
        category="element",
        properties={
            "target_id": {"type": "string"},
            "end_dx": {
                "type": "number",
                "minimum": -3000,
                "maximum": 3000,
                "description": "终点相对目标中心的水平 CSS 像素偏移",
            },
            "end_dy": {
                "type": "number",
                "minimum": -3000,
                "maximum": 3000,
                "description": "终点相对目标中心的垂直 CSS 像素偏移",
            },
            "duration_ms": {
                "type": "integer",
                "minimum": 100,
                "maximum": 5000,
            },
            "steps": {"type": "integer", "minimum": 2, "maximum": 120},
            "security_challenge": {
                "type": "boolean",
                "description": "目标属于验证码或真人验证时必须为 true",
            },
            "expect_kind": {
                "type": "string",
                "enum": [
                    "challenge_ready",
                    "url_contains",
                    "title_contains",
                    "text_contains",
                    "target_exists",
                ],
            },
            "expect_value": {"type": "string"},
        },
        required=(
            "target_id",
            "end_dx",
            "end_dy",
            "duration_ms",
            "steps",
            "security_challenge",
            "expect_kind",
            "expect_value",
        ),
        returns="拖拽回执与业务后置条件校验结果",
        requires_observation=True,
        idempotent=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="click",
        description=(
            "点击当前观察中的目标区域，并校验业务结果。"
            "button 可选 right 打开右键菜单、middle 中键点击；click_count 传 2 表示双击。"
            "expect_kind/expect_value 可省略，缺省按“页面有变化”校验（自动绑定当前观察）；"
            "知道业务结果时优先给 url_contains / text_contains，判据更强"
        ),
        category="element",
        properties={
            "target_id": {"type": "string"},
            "button": POINTER_BUTTON_PROPERTY,
            "click_count": CLICK_COUNT_PROPERTY,
            "expect_kind": {
                "type": "string",
                "enum": [
                    "url_contains",
                    "title_contains",
                    "text_contains",
                    "target_exists",
                    "fingerprint_changed",
                ],
            },
            "expect_value": {"type": "string"},
        },
        required=("target_id",),
        returns=(
            "点击回执与业务后置条件校验结果，附动作后的新页面观察 page；"
            "只读语义点击失败后可有界重试"
        ),
        requires_observation=True,
        idempotent=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="input_text",
        description=(
            "向目标输入文本并回读确认写入。敏感值（账号、密码、令牌）只能用 input_key 引用"
            "任务输入，明文不进参数；搜索词、备注这类非敏感字面量直接给 text。"
            "input_key 与 text 二选一"
        ),
        category="element",
        properties={
            "target_id": {"type": "string"},
            "input_key": {"type": "string"},
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_FORM_TEXT_LENGTH,
                "description": "非敏感字面量；账号、密码、令牌必须改用 input_key",
            },
        },
        required=("target_id",),
        returns="输入回执与回读校验结果，附动作后的新页面观察 page；轨迹中不保存输入值",
        requires_observation=True,
        idempotent=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="input_generated_text",
        description=(
            "把模型从当前多模态截图识别出的非敏感短文本输入目标；"
            "不得用于账号、密码或其他任务输入，不会写入快速路径"
        ),
        category="element",
        properties={
            "target_id": {"type": "string"},
            "text": {"type": "string", "minLength": 1, "maxLength": 128},
            "observation_fingerprint": {"type": "string"},
            "screenshot_fingerprint": {"type": "string"},
            "visual_confidence": {"type": "number", "minimum": 0.8, "maximum": 1},
            "security_challenge": {
                "type": "boolean",
                "description": "文本来自验证码或真人验证图片时必须为 true",
            },
        },
        required=(
            "target_id",
            "text",
            "observation_fingerprint",
            "screenshot_fingerprint",
            "visual_confidence",
            "security_challenge",
        ),
        returns="输入回执与回读校验结果；同一挑战现场的等价重复会被拒绝",
        requires_observation=True,
        idempotent=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="select",
        description=(
            "选择下拉值并校验结果；值可来自安全静态值或任务输入键。"
            "expect_kind/expect_value 可省略，缺省按“页面有变化”校验"
        ),
        category="element",
        properties={
            "target_id": {"type": "string"},
            "value": {"type": "string"},
            "input_key": {"type": "string"},
            "expect_kind": {"type": "string"},
            "expect_value": {
                "type": "string",
                "description": "target_exists 时必须填写当前观察中的 target_id",
            },
        },
        required=("target_id",),
        returns="选择回执与业务后置条件校验结果",
        requires_observation=True,
        idempotent=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="scroll",
        description="按像素垂直滚动页面",
        category="page",
        properties={"amount": {"type": "number"}},
        required=("amount",),
        returns="滚动回执；正值向下，负值向上",
    ),
    ToolDefinition(
        name="wait",
        description="短暂等待页面异步变化，最长 10 秒",
        category="page",
        properties={"seconds": {"type": "number", "minimum": 0, "maximum": 10}},
        required=("seconds",),
        returns="等待回执；不检查任何页面条件",
    ),
    ToolDefinition(
        name="wait_until",
        description=(
            "等待可验证的页面或业务条件；等待期间由代码检查，不持续调用模型，"
            "进程中断后可从检查点恢复"
        ),
        category="page",
        properties={
            "reason": {"type": "string"},
            "expect_kind": {
                "type": "string",
                "enum": [
                    "url_contains",
                    "title_contains",
                    "text_contains",
                    "target_exists",
                ],
            },
            "expect_value": {"type": "string"},
            "timeout_seconds": {
                "type": "number",
                "minimum": 1,
                "maximum": 86400,
            },
            "poll_interval_seconds": {
                "type": "number",
                "minimum": 0.01,
                "maximum": 30,
            },
        },
        required=(
            "reason",
            "expect_kind",
            "expect_value",
            "timeout_seconds",
            "poll_interval_seconds",
        ),
        returns="进入 WAITING 状态并保存检查点，由智能体循环在条件命中后唤醒",
        externally_callable=False,
        unavailable_reason=f"{_ENGINE_ONLY}；外部等待请使用 wait 或 wait_network_response",
        counts_as_action=False,
    ),
    ToolDefinition(
        name="inspect_collection_structure",
        description=(
            "只读分析当前页面中重复出现的列表、表格或卡片结构；"
            "返回候选 CSS 行选择器和字段提示，不逐条输出业务数据"
        ),
        category="collection",
        properties={},
        returns="结构候选列表，含 candidate_id、行选择器、字段提示和分页提示",
        counts_as_action=False,
    ),
    ToolDefinition(
        name="run_structured_extraction",
        description=(
            "从上一轮结构观察中选择 candidate_id；代码复用已验证的字段与分页提示，"
            "批量采集、去重、校验并导出私有 JSON/CSV。用户要求每条详情时，"
            "从候选 detail_hints 选择 detail_field_id，由代码逐条合并。"
            "不要生成 CSS 或逐条复述数据"
        ),
        category="collection",
        properties={
            "collection_name": {"type": "string", "minLength": 1, "maxLength": 100},
            "candidate_id": {
                "type": "string",
                "description": "上一轮 inspect_collection_structure 返回的候选 ID",
            },
            "unique_field_id": {
                "type": "string",
                "description": "可选；候选中的稳定唯一字段 ID，省略时由代码选择",
            },
            "detail_field_id": {
                "type": "string",
                "description": ("可选；用户要求逐条详情时，选择候选 detail_hints 中的详情入口 ID"),
            },
            "filters": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "field_id": {"type": "string"},
                        "operator": {
                            "type": "string",
                            "enum": [
                                "equals",
                                "not_equals",
                                "contains",
                                "starts_with",
                                "ends_with",
                            ],
                        },
                        "value": {"type": ["string", "number", "integer", "boolean"]},
                    },
                    "required": ["field_id", "operator", "value"],
                    "additionalProperties": False,
                },
            },
            "max_pages": {"type": "integer", "minimum": 1, "maximum": 500},
            "max_items": {"type": "integer", "minimum": 1, "maximum": 100000},
        },
        required=("collection_name", "candidate_id"),
        returns=(
            "采集计数、页数、完整性证据和私有 JSON/CSV 产物路径；不返回业务记录。"
            "成功且强证据时会经存储前验证门晋升为可重放采集程序"
        ),
        idempotent=False,
    ),
    ToolDefinition(
        name="replay_collection_program",
        description=(
            "按当前页面与任务场景查询已验证采集程序；入口结构探针通过后零模型重放整页采集。"
            "失配时返回明确原因并降权程序，调用方应回退到 inspect_collection_structure +"
            " run_structured_extraction 重新编译"
        ),
        category="collection",
        properties={},
        returns="重放成功时返回与 run_structured_extraction 相同的完整性摘要；失配时 success=false",
        idempotent=False,
        counts_as_action=True,
        requires_observation=False,
    ),
    ToolDefinition(
        name="screenshot",
        description="保存当前页面证据截图",
        category="page",
        properties={"label": {"type": "string"}},
        required=("label",),
        returns="证据文件引用；文件权限为 0600 且敏感字段已脱敏",
    ),
    ToolDefinition(
        name="read_page_markdown",
        description=(
            "把当前页面的主内容转成 Markdown 交给调用方的模型阅读：自动挑出正文容器、"
            "剥掉导航、页眉页脚与侧栏，保留标题层级、列表、代码块、表格与行内链接。"
            "读文档、读文章、读详情页正文用这个，不要退化成逐个元素 read_element。"
            "重复出现的结构化记录仍然必须走结构化采集——Markdown 没有去重、"
            "分页闭合与完整性证据"
        ),
        category="page",
        properties={
            "only_main_content": {
                "type": "boolean",
                "description": "默认真，剥离导航与页脚；传假则从 body 整体转换",
            },
            "selector": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "显式指定正文容器的 CSS 选择器，优先于自动判定",
            },
            "include_links": {"type": "boolean", "description": "默认真，保留行内链接"},
            "include_images": {"type": "boolean", "description": "默认假，保留图片引用"},
            "max_chars": {
                "type": "integer",
                "minimum": 1000,
                "maximum": MAX_PAGE_MARKDOWN_CHARS,
                "description": f"Markdown 字符上限，默认 {DEFAULT_PAGE_MARKDOWN_CHARS}",
            },
        },
        returns=(
            "markdown 正文、是否按上限截断、字符数与页面真实总长、标题、地址与判定出的正文容器"
        ),
        counts_as_action=False,
    ),
    ToolDefinition(
        name="check_crawl_policy",
        description=(
            "读取目标站点的 robots.txt 并判定该地址是否允许自动抓取，"
            "同时给出站点声明的 Crawl-delay 与 Sitemap 地址。"
            "遍历站内多个页面前先查这个；判定只是结论，是否据此停下由调用方决定——"
            "除非会话装配时打开了 respect_robots，那时导航会按判定被硬拦。"
            "robots.txt 返回 4xx 视为全站放行，5xx 或取不到视为状态未知而不是放行"
        ),
        category="page",
        properties={
            "url": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "description": "要判定的地址；省略时用当前页面地址",
            },
            "agent": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": (
                    f"用于匹配 robots.txt 分组的 User-agent 名，默认 {DEFAULT_CRAWL_AGENT}"
                ),
            },
            "refresh": {
                "type": "boolean",
                "description": "为真时忽略本会话缓存，重新读取 robots.txt",
            },
        },
        returns=(
            "allowed 为 true/false/null，null 表示未知；命中的规则、站点声明的 Crawl-delay 与 "
            "Sitemap 列表、当前生效的请求间隔，以及是否命中会话缓存"
        ),
        counts_as_action=False,
    ),
    ToolDefinition(
        name="list_page_links",
        description=(
            "列出当前页面的全部链接，地址统一换算成绝对地址并按出现顺序去重；"
            "可只看同源链接或按子串筛选，也可一并列出图片。"
            "这是调用方自行编排站内遍历的起点：先列链接，再逐个导航并 read_page_markdown"
        ),
        category="page",
        properties={
            "same_origin_only": {
                "type": "boolean",
                "description": "默认假；为真时只保留与当前页面同源的链接",
            },
            "contains": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "只保留地址或链接文本包含该子串的链接",
            },
            "include_images": {"type": "boolean", "description": "默认假，附带图片地址与 alt"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PAGE_LINKS,
                "description": f"最多返回多少条，默认 {DEFAULT_PAGE_LINKS}",
            },
        },
        returns="链接列表，每项含绝对地址、链接文本、是否同源与 rel/target；另有扫描与返回计数",
        counts_as_action=False,
    ),
    ToolDefinition(
        name="capture_annotated_screenshot",
        description=(
            "在当前视口截图上叠加编号方框，编号与观察候选的 target_id 一一对应，"
            "供多模态调用方先看图、再按编号取 target_id 操作。"
            "只往页面追加一层不可点击的临时覆盖层并在截图后立即移除，"
            "不改变业务状态也不滚动页面；完全在视口外的候选不会入图例"
        ),
        category="page",
        properties={
            "label": {"type": "string", "minLength": 1, "maxLength": 100},
            "max_labels": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_ANNOTATION_LABELS,
                "description": f"最多标注多少个候选，默认 {DEFAULT_ANNOTATION_LABELS}",
            },
            "roles": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 40},
                "maxItems": 20,
                "description": '只标注某几类角色，例如 ["button", "textbox"]',
            },
        },
        returns=(
            "截图路径与图例；图例每项含编号、target_id、role、name 与视口包围盒，"
            "另有候选总数与实际入图数量"
        ),
        counts_as_action=False,
    ),
    ToolDefinition(
        name="finish",
        description="仅在任务目标已通过页面证据确认后完成任务",
        category="lifecycle",
        properties={
            "summary": {"type": "string"},
            "memories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "attention",
                                "load_condition",
                                "locator",
                                "recovery",
                                "navigation",
                                "data_hint",
                            ],
                        },
                        "content": {"type": "object"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["kind", "content"],
                },
            },
        },
        required=("summary",),
        returns="经完成门校验后进入 COMPLETED，并写回有证据的路径记忆",
        externally_callable=False,
        unavailable_reason=_ENGINE_ONLY,
        counts_as_action=False,
    ),
    ToolDefinition(
        name="ask_user",
        description=(
            "仅当缺少无法从页面、任务输入或工具结果推导的业务事实，或存在不可逆业务选择时"
            "向用户提出一个具体问题；不得用于技术不确定、页面仍可观察、等待模型或已有输入"
        ),
        category="lifecycle",
        properties={
            "question": {"type": "string", "minLength": 1, "maxLength": 500},
            "reason": {
                "type": "string",
                "enum": [
                    "ambiguous_goal",
                    "missing_business_fact",
                    "irreversible_choice",
                ],
            },
            "input_key": {
                "type": "string",
                "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$",
                "description": ("保存下一条用户回答的稳定任务输入键，例如 phone 或 order_number"),
            },
            "answer_type": {
                "type": "string",
                "enum": ["text", "identifier", "number", "choice", "secret"],
                "description": "回答的预期类型；手机号、订单号等单值使用 identifier",
            },
        },
        required=("question", "reason", "input_key", "answer_type"),
        returns="进入 user_message 等待，收到当前任务回答后沿用原检查点继续",
        externally_callable=False,
        unavailable_reason=_ENGINE_ONLY,
        counts_as_action=False,
    ),
    ToolDefinition(
        name="block",
        description="权限、验证码、MFA、凭据或安全策略使任务无法继续时停止",
        category="lifecycle",
        properties={"reason": {"type": "string"}},
        required=("reason",),
        returns="进入 BLOCKED 终态并保留检查点",
        externally_callable=False,
        unavailable_reason=_ENGINE_ONLY,
        counts_as_action=False,
    ),
)

NETWORK_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="inspect_network_data",
        description=(
            "只读列出代码捕获的 JSON 接口候选；只返回去查询参数的路径、状态、"
            "大小和字段结构，不返回响应记录值"
        ),
        category="network",
        properties={"max_candidates": {"type": "integer", "minimum": 1, "maximum": 50}},
        returns="候选列表，含 candidate_id、endpoint、状态、字节数和字段结构摘要",
        counts_as_action=False,
    ),
    ToolDefinition(
        name="export_network_response",
        description=(
            "选择一个或多个已观察的同接口候选，由代码聚合、去重并导出私有 JSON；"
            "识别到记录数组时同时导出 CSV，模型只接收计数和产物路径。"
            "多个分页响应只有在声明总数或总页数闭合时才会被标记完整"
        ),
        category="network",
        properties={
            "candidate_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "candidate_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 80},
                "minItems": 1,
                "maxItems": 50,
                "uniqueItems": True,
            },
            "collection_name": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        required=("collection_name",),
        returns="记录数、完整性证据和私有 JSON/CSV 产物路径；不返回业务记录",
    ),
    ToolDefinition(
        name="wait_network_response",
        description=(
            "在页面动作已经触发请求后，等待匹配 URL 子串的网络响应到达；"
            "由代码等待，不占用模型轮次。命中授权 origin 内的 2xx JSON 时"
            "返回可直接导出的 candidate_id，其余情况返回状态和类型元数据，"
            "超时返回未匹配"
        ),
        category="network",
        properties={
            "url_substring": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "接口 URL 中的稳定子串，例如 /api/order/list",
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 1,
                "maximum": 300,
            },
        },
        required=("url_substring",),
        returns="匹配结果、状态、耗时；命中捕获时附带可导出的 candidate_id",
        counts_as_action=False,
    ),
    ToolDefinition(
        name="manage_network_route",
        description=(
            "管理当前任务允许 origin 内的网络路由。list 只读查看；add 可阻断请求、"
            "修改请求、返回模拟响应或替换响应；remove 删除已安装规则。"
        ),
        category="network",
        properties={
            "operation": {"type": "string", "enum": ["list", "add", "remove"]},
            "rule_id": {"type": "string", "maxLength": 80},
            "url_pattern": {"type": "string", "maxLength": 2048},
            "action": {
                "type": "string",
                "enum": ["block", "modify_request", "mock_response", "modify_response"],
            },
            "method": {"type": "string", "maxLength": 20},
            "request_headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
            "request_header_input_keys": {
                "type": "object",
                "description": (
                    "请求 Header 名到任务 input_key 的映射。Authorization、Cookie、"
                    "Host、令牌和 API Key 等敏感值必须通过此字段注入；Host 会按 Chromium "
                    "约束编译为同路径、同查询的请求 URL authority 重写"
                ),
                "additionalProperties": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 100,
                },
            },
            "request_method": {"type": "string", "maxLength": 20},
            "request_body": {"type": "string", "maxLength": 16000},
            "response_status": {"type": "integer", "minimum": 100, "maximum": 599},
            "response_headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
            "response_header_input_keys": {
                "type": "object",
                "description": ("响应 Header 名到任务 input_key 的映射，敏感值只在执行层解析"),
                "additionalProperties": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 100,
                },
            },
            "response_body": {"type": "string", "maxLength": 16000},
        },
        required=("operation",),
        returns="当前规则摘要；敏感 Header 值不回显，最多保留 8 条规则",
    ),
)

DIAGNOSTIC_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="inspect_page_diagnostics",
        description=(
            "只读检查页面就绪状态、焦点、控制台异常和失败网络请求；"
            "动作没有产生预期结果或页面行为不明时主动调用"
        ),
        category="diagnostics",
        properties={
            "max_console": {"type": "integer", "minimum": 1, "maximum": 50},
            "max_network": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        returns="页面就绪状态、控制台异常计数、失败请求分类；正文与堆栈不外发",
        counts_as_action=False,
    ),
)

LOCATOR_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="click_locator",
        description=(
            "候选 target_id 不可用时，以 CSS、XPath、role、text、label 或 test_id 定位并点击；"
            "同样支持 button 与 click_count。expect_kind/expect_value 可省略，缺省按“页面有变化”校验"
        ),
        category="element",
        properties={
            "locator": LOCATOR_PROPERTY,
            "button": POINTER_BUTTON_PROPERTY,
            "click_count": CLICK_COUNT_PROPERTY,
            "expect_kind": {
                "type": "string",
                "enum": [
                    "url_contains",
                    "title_contains",
                    "text_contains",
                    "target_exists",
                    "fingerprint_changed",
                ],
            },
            "expect_value": {"type": "string"},
        },
        required=("locator",),
        returns="点击回执与业务后置条件校验结果；多匹配未给 index 时停止消歧",
        idempotent=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="input_text_locator",
        description=(
            "候选 target_id 不可用时，以显式定位器找到输入框并写入。"
            "敏感值用 input_key 引用任务输入，非敏感字面量直接给 text；二选一"
        ),
        category="element",
        properties={
            "locator": LOCATOR_PROPERTY,
            "input_key": {"type": "string"},
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_FORM_TEXT_LENGTH,
                "description": "非敏感字面量；账号、密码、令牌必须改用 input_key",
            },
        },
        required=("locator",),
        returns="输入回执与回读校验结果；轨迹中不保存输入值",
        idempotent=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="select_locator",
        description=(
            "候选 target_id 不可用时，以显式定位器找到下拉框并选择任务值或静态值。"
            "expect_kind/expect_value 可省略，缺省按“页面有变化”校验"
        ),
        category="element",
        properties={
            "locator": LOCATOR_PROPERTY,
            "value": {"type": "string"},
            "input_key": {"type": "string"},
            "expect_kind": {"type": "string"},
            "expect_value": {"type": "string"},
        },
        required=("locator",),
        returns="选择回执与业务后置条件校验结果",
        idempotent=False,
        requires_write_permission=True,
    ),
)

ELEMENT_READ_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="read_element",
        description=(
            "只读读取单个元素的标签、角色、名称、文本、表单值、可见性、边界框和白名单属性；"
            "用观察候选的 target_id 或显式定位器指定目标，不改变页面状态。"
            "密码类控件只返回长度，批量列表数据必须使用结构化采集而不是逐个读取"
        ),
        category="element",
        properties={
            "target_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "locator": LOCATOR_PROPERTY,
            "max_text_length": {"type": "integer", "minimum": 0, "maximum": 20000},
            "include_html": {"type": "boolean"},
        },
        returns=("元素白名单状态；文本按上限截断并标记是否截断，密码值只返回长度"),
        counts_as_action=False,
    ),
    ToolDefinition(
        name="capture_element_screenshot",
        description=(
            "只截取单个元素所在矩形并落盘，用观察候选的 target_id 或显式定位器指定目标；"
            "元素在视口外也不滚动页面，适合验证码、图表和局部取证"
        ),
        category="element",
        properties={
            "target_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "locator": LOCATOR_PROPERTY,
            "label": {"type": "string", "minLength": 1, "maxLength": 60},
            "padding": {"type": "number", "minimum": 0, "maximum": 200},
        },
        returns="截图文件路径、元素视口包围盒与实际截取的页面坐标裁剪区",
        counts_as_action=False,
    ),
)

POINTER_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="hover",
        description=(
            "把鼠标悬停到目标上并校验业务结果，用观察候选的 target_id 或显式定位器指定目标；"
            "用于展开悬停菜单、触发提示气泡这类只有 mouseover 才会出现的内容。"
            "expect_kind/expect_value 可省略，缺省按“页面有变化”校验"
        ),
        category="element",
        properties={
            "target_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "locator": LOCATOR_PROPERTY,
            "expect_kind": {
                "type": "string",
                "enum": [
                    "url_contains",
                    "title_contains",
                    "text_contains",
                    "target_exists",
                    "fingerprint_changed",
                ],
            },
            "expect_value": {"type": "string"},
        },
        required=(),
        returns="悬停回执与业务后置条件校验结果；悬停不点击，不会提交任何业务写操作",
        idempotent=True,
    ),
)

_TRAFFIC_FILTER_PROPERTIES: dict[str, Any] = {
    "url_contains": {
        "type": "string",
        "minLength": 1,
        "maxLength": 500,
        "description": "按 URL 子串过滤，例如 /api/order",
    },
    "methods": {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 20},
        "maxItems": 20,
        "description": '按 HTTP 方法过滤，例如 ["POST"]',
    },
    "resource_types": {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 30},
        "maxItems": 20,
        "description": '按 CDP 资源类型过滤，例如 ["XHR", "Fetch", "WebSocket"]',
    },
    "status_min": {"type": "integer", "minimum": 100, "maximum": 599},
    "status_max": {"type": "integer", "minimum": 100, "maximum": 599},
    "only_failed": {"type": "boolean", "description": "只返回失败或被阻断的交换"},
}

TRAFFIC_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="inspect_network_traffic",
        description=(
            "列出当前受管浏览器已发生的网络交换，覆盖全部资源类型：请求行、请求头、"
            "响应头、状态、MIME、协议、远端地址、时序分解、发起方调用栈、"
            "字节数与 WebSocket 帧数。这是排查接口报错、找出真实接口地址、"
            "确认请求头是否带上鉴权信息的首选工具。"
            "调用方拿到完整 Header 值，模型侧只看到 Header 名称与脱敏 URL"
        ),
        category="network",
        properties={
            **_TRAFFIC_FILTER_PROPERTIES,
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        returns=(
            "exchanges 列表与缓冲区统计，每条含 exchange_id、完整 URL、请求头、响应头、"
            "timing 和 initiator；exchange_id 可直接用于读取正文、导出 HAR 或重放"
        ),
        counts_as_action=False,
    ),
    ToolDefinition(
        name="read_network_body",
        description=(
            "按 exchange_id 读取某次交换的请求体或响应体原文；"
            "正文是 JSON 时额外返回已解析结构。二进制按 base64 返回，"
            "超过单体上限时只返回长度与原因"
        ),
        category="network",
        properties={
            "exchange_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "part": {"type": "string", "enum": ["request", "response"]},
        },
        required=("exchange_id",),
        returns="正文文本或 base64、字节数、是否截断；JSON 正文附带解析后的对象",
        counts_as_action=False,
    ),
    ToolDefinition(
        name="search_network_traffic",
        description=(
            "在已抓取的响应体、请求体、Header 值、WebSocket 帧与 SSE 消息里按子串全文搜索，"
            "定位页面上看到的某个值(订单号、价格、token)来自哪次交换。"
            "正文本就在内存里，无需逐条 read_network_body。返回命中交换的 exchange_id "
            "与片段，可直接接 read_network_body 或 analyze_api_endpoint"
        ),
        category="network",
        properties={
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "要搜索的子串，例如订单号或 token 前缀",
            },
            "scope": {
                "type": "string",
                "enum": [
                    "response_body",
                    "request_body",
                    "body",
                    "headers",
                    "websocket",
                    "sse",
                    "all",
                ],
                "description": "搜索范围，默认 body，即请求体加响应体",
            },
            "case_sensitive": {"type": "boolean"},
            "url_contains": {"type": "string", "minLength": 1, "maxLength": 500},
            "resource_types": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 30},
                "maxItems": 20,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        required=("query",),
        returns=(
            "命中列表，每条含 exchange_id、URL、命中部位、命中次数与上下文片段；"
            "模型侧只拿交换定位与命中次数，不含片段"
        ),
        counts_as_action=False,
    ),
    ToolDefinition(
        name="export_network_har",
        description=(
            "把过滤后的流量导出为 HAR 1.2 私有文件，可直接导入 Reqable、Charles "
            "或浏览器开发者工具二次分析；WebSocket 连接与帧写入 HAR 扩展字段"
        ),
        category="network",
        properties={
            **_TRAFFIC_FILTER_PROPERTIES,
            "collection_name": {"type": "string", "minLength": 1, "maxLength": 100},
            "include_bodies": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20000},
        },
        required=("collection_name",),
        returns="HAR 文件路径、条目数、WebSocket 数与字节数；不返回正文本身",
        counts_as_action=False,
    ),
    ToolDefinition(
        name="replay_network_request",
        description=(
            "重放或编辑重发一次请求：给 exchange_id 即按原样重放，"
            "再给 url/method/headers/body/remove_headers 即在原请求基础上逐项覆盖。"
            "请求复用当前浏览器会话与 Cookie，由固定模板在页面上下文发起，"
            "浏览器禁止脚本设置的 Header 由一次性拦截补齐。"
            "这是非幂等动作，失败不会自动重试"
        ),
        category="network",
        properties={
            "exchange_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
                "description": "来源交换；省略时必须提供完整 url",
            },
            "url": {"type": "string", "minLength": 1, "maxLength": 4096},
            "method": {"type": "string", "minLength": 1, "maxLength": 20},
            "headers": {
                "type": "object",
                "description": "覆盖或新增的请求头；键为 Header 名，值为文本",
            },
            "remove_headers": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 100},
                "maxItems": 50,
                "description": "从来源请求中删除的 Header 名",
            },
            "body": {"type": "string", "maxLength": 2097152},
            "referrer": {"type": "string", "minLength": 1, "maxLength": 4096},
        },
        returns="状态码、响应头、响应正文与耗时；JSON 正文附带解析后的对象",
        idempotent=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="read_websocket_frames",
        description=(
            "读取某个 WebSocket 连接已收发的帧内容，可按方向与子串过滤。"
            "帧既不是请求体也不是响应体，read_network_body 对 WebSocket 交换必然落空，"
            "实时行情、聊天、推送这类接口只能用本工具看内容。"
            "帧正文是 JSON 时额外返回已解析结构"
        ),
        category="network",
        properties={
            "exchange_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "direction": {
                "type": "string",
                "enum": ["sent", "received"],
                "description": "只看某一方向；省略表示两个方向都要",
            },
            "contains": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "只保留帧正文包含该子串的帧",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "最多返回多少帧，默认 100，取最新的一段",
            },
        },
        required=("exchange_id",),
        returns=(
            "帧列表，每帧含方向、opcode、字节数、时间戳与正文，JSON 正文附带解析后的对象；"
            "另有方向与 opcode 分布统计。模型侧只拿统计，不含帧正文"
        ),
        counts_as_action=False,
    ),
    ToolDefinition(
        name="read_sse_messages",
        description=(
            "读取某个 text/event-stream (SSE) 连接已收到的消息，可按事件名与子串过滤。"
            "SSE 连接常年不关闭，read_network_body 读不到内容；"
            "LLM 流式对话、服务端通知推送这类接口只能用本工具看内容。"
            "消息正文是 JSON 时额外返回已解析结构"
        ),
        category="network",
        properties={
            "exchange_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "event_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "只看某个事件名的消息；省略表示全部",
            },
            "contains": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "只保留正文包含该子串的消息",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "最多返回多少条，默认 100，取最新的一段",
            },
        },
        required=("exchange_id",),
        returns=(
            "消息列表，每条含事件名、事件 ID、字节数、时间戳与正文，JSON 正文附带解析后的对象；"
            "另有事件名分布统计。模型侧只拿统计，不含消息正文"
        ),
        counts_as_action=False,
    ),
    ToolDefinition(
        name="analyze_api_endpoint",
        description=(
            "把同一个接口的多次交换归纳成可直接照着写代码的契约："
            "参数化后的 URL 模板；query 参数表，含推断类型、示例与是否每次都变；"
            "凭据位置，含 Authorization 方案、Cookie 名、鉴权 Header 与签名参数；"
            "请求体结构，JSON schema 或表单字段或 GraphQL 操作名与变量；"
            "响应结构与批量数据所在的 record_path，以及分页策略。"
            "想把页面上看到的数据改成直接调接口拿时，先用本工具"
        ),
        category="network",
        properties={
            "exchange_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
                "description": "来自 inspect_network_traffic 的交换标识；与 url_contains 二选一",
            },
            "url_contains": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "按 URL 子串挑选接口，优先取成功响应中最新的一条",
            },
        },
        required=(),
        returns=(
            "endpoint 含 method、url_template、sample_url；另有 query_params、auth、"
            "request_body、pagination，以及 response 下的 schema、record_path 与 "
            "total_fields；模型侧不含 Header 值、凭据与响应取值"
        ),
        counts_as_action=False,
    ),
    ToolDefinition(
        name="collect_api_pages",
        description=(
            "沿分页把整个接口的数据一次取全，不必在页面上一页页点。"
            "复用来源请求的登录态与 Header，逐页在浏览器会话内发起，"
            "支持 page_number、offset、cursor 三种策略，缺省从接口契约自动推断。"
            "分页字段既可在 query 也可在 POST 的 JSON/表单请求体里(page_in=body)；"
            "游标既可来自响应正文，也可来自响应头(cursor_in=header)或 Link 头(cursor_in=link)。"
            "记录按内容或指定键去重；某页全是重复即判定服务端忽略了分页参数并停下。"
            "结果必须自带闭合证据：收齐数与服务端声明的总数一致，或末页确实短于整页，"
            "才会 closed=true；只是跑到页数上限一律 closed=false 并说明缺口。"
            "这是非幂等动作，失败不会自动重试"
        ),
        category="network",
        properties={
            "exchange_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
                "description": "作为遍历起点的交换；与 url_contains 二选一",
            },
            "url_contains": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "按 URL 子串挑选接口，优先取成功响应中最新的一条",
            },
            "strategy": {
                "type": "string",
                "enum": list(PAGINATION_STRATEGIES),
                "description": "覆盖自动推断的分页策略",
            },
            "page_param": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": (
                    "承载页码、偏移量或游标的参数名；page_in=body 时可用点号表示"
                    "嵌套 JSON 路径，例如 query.pageNum"
                ),
            },
            "page_in": {
                "type": "string",
                "enum": list(PAGE_LOCATIONS),
                "description": (
                    "分页字段位置，默认 query 改写 URL；body 改写 POST 的 JSON 或表单请求体，"
                    "字段原有类型会保留"
                ),
            },
            "cursor_in": {
                "type": "string",
                "enum": list(CURSOR_SOURCES),
                "description": (
                    "cursor 策略下游标的来源，默认 body 从响应正文取；header 从 cursor_header "
                    "指定的响应头取；link 直接用 Link: rel=next 给出的下一页 URL"
                ),
            },
            "cursor_header": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "cursor_in=header 时承载游标的响应头名，例如 X-Next-Cursor",
            },
            "start": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1_000_000,
                "description": "起始页码或偏移量；缺省沿用样本 URL 或请求体自己的取值",
            },
            "step": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10_000,
                "description": "offset 策略每页前进量；缺省等于每页大小",
            },
            "page_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10_000,
                "description": "每页条数；缺省从样本 URL 的 size/limit 参数读取",
            },
            "record_path": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 100},
                "maxItems": 8,
                "description": "记录数组在响应里的路径；缺省用接口契约推断的 record_path",
            },
            "total_path": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 100},
                "maxItems": 8,
                "description": "总数字段路径；缺省在响应顶层找 total 一类字段",
            },
            "cursor_field": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "cursor 策略下从响应哪个字段取下一个游标",
            },
            "dedupe_key": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "记录去重字段；缺省按记录内容整体去重",
            },
            "max_pages": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PAGINATION_PAGES,
                "description": f"最多翻多少页，缺省 {DEFAULT_PAGINATION_PAGES}",
            },
            "delay_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": 10_000,
                "description": "页间间隔毫秒，用于避让服务端速率限制",
            },
        },
        required=(),
        returns=(
            "closed 与 reason 给出是否取全及判据；declared_total、collected、"
            "pages_fetched、failed_pages 为闭合证据；plan 回显实际使用的遍历计划；"
            "records 是全部记录，只回给调用方进程，模型侧只见计数与闭合结论"
        ),
        idempotent=False,
        counts_as_action=False,
    ),
    ToolDefinition(
        name="export_request_code",
        description=(
            "把一次捕获的请求导出成可独立运行的调用代码，支持 curl、Python requests、"
            "Python httpx、浏览器 fetch 与 Node axios。Cookie、Authorization、API Key "
            "一类凭据默认替换成环境变量占位并单独列出，不写进代码明文；"
            "正文按 Content-Type 选择正确的传参方式，即 json、data 或原文"
        ),
        category="network",
        properties={
            "exchange_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "target": {
                "type": "string",
                "enum": list(CODE_TARGETS),
                "description": "目标语言或工具，默认 curl",
            },
            "include_secrets": {
                "type": "boolean",
                "description": (
                    "为真时把凭据明文写进代码；只有外部调用方在受控环境中才应开启，"
                    "默认关闭并使用环境变量占位"
                ),
            },
        },
        required=("exchange_id",),
        returns="可运行代码文本、目标语言、正文类型与占位环境变量清单",
        counts_as_action=False,
    ),
)

FORM_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="fill_form",
        description=(
            "一次调用写入多个表单字段，逐字段回读真实值校验。"
            "同一次观察的多个 target_id 在连续写入后仍然有效，因此整张表单只需观察一次。"
            "填表单不改变页面指纹，所以这里不接受也不需要页面级后置条件——"
            "每个字段的成功判据是它自己的值确实被写进去了。"
            "敏感值用 input_key 引用任务输入，明文不进工具参数与轨迹"
        ),
        category="form",
        properties={
            "fields": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_FORM_FIELDS,
                "items": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": "string", "maxLength": 200},
                        "locator": LOCATOR_PROPERTY,
                        "input_key": {
                            "type": "string",
                            "maxLength": 100,
                            "description": "敏感值对应的任务输入键",
                        },
                        "text": {
                            "type": "string",
                            "maxLength": MAX_FORM_TEXT_LENGTH,
                            "description": "非敏感字面量文本",
                        },
                        "select_value": {
                            "type": "string",
                            "maxLength": 300,
                            "description": "下拉框选项；按 value、label 或可见文本任一匹配",
                        },
                        "checked": {
                            "type": "boolean",
                            "description": "勾选框的目标状态",
                        },
                    },
                    "additionalProperties": False,
                },
                "description": (
                    "每个字段给出 target_id 或 locator 之一定位，"
                    "并给出 input_key、text、select_value、checked 之一作为写入值"
                ),
            },
        },
        required=("fields",),
        returns="逐字段的写入与回读结果；某个字段失败不影响其余字段继续写入",
        requires_observation=True,
        idempotent=True,
        counts_as_action=True,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="wait_for_condition",
        description=(
            "等待页面出现可验证的条件，等待期间由代码轮询，不消耗模型调用。"
            "用于异步加载、跳转与后台任务完成；条件满足即刻返回，超时返回未满足"
        ),
        category="page",
        properties={
            "expect_kind": {
                "type": "string",
                "enum": ["url_contains", "title_contains", "text_contains", "fingerprint_changed"],
            },
            "expect_value": {"type": "string", "maxLength": 300},
            "timeout_seconds": {
                "type": "number",
                "minimum": 0.1,
                "maximum": 300,
                "description": "默认 10 秒",
            },
        },
        required=("expect_kind", "expect_value"),
        returns="是否满足、实际等待秒数与未满足时的原因",
        requires_observation=False,
        idempotent=True,
        counts_as_action=False,
    ),
    ToolDefinition(
        name="manage_storage_state",
        description=(
            "整体导出或导入会话态，含 Cookie、localStorage 与 sessionStorage，用于跳过重复登录。"
            "导出会写入一个 0600 私有文件并返回路径；导入接受该文件或同结构对象。"
            "快照结构与 Playwright 的 storageState 一致，可以互相喂。"
            "导入时越出任务授权 origin 的条目会被跳过"
        ),
        category="storage",
        properties={
            "operation": {"type": "string", "enum": ["export", "import"]},
            "file_path": {
                "type": "string",
                "maxLength": 1024,
                "description": "导入时读取的会话态文件路径",
            },
            "state": {
                "type": "object",
                "description": "导入时直接给出的会话态对象，与 file_path 二选一",
            },
            "clear_existing": {
                "type": "boolean",
                "description": "导入前先清空当前页面的 Web Storage",
            },
        },
        required=("operation",),
        returns="导出返回快照与文件路径，模型侧只见摘要；导入返回生效与跳过的条目数",
        requires_observation=False,
        idempotent=True,
        counts_as_action=False,
        requires_write_permission=True,
    ),
)

PAGE_EXTRA_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="drag_to_element",
        description=(
            "把一个元素拖到另一个元素上，用于排序、看板换列、拖进文件夹这类操作。"
            "自动识别页面用的是 HTML5 原生拖放还是鼠标事件拖放并走对应通道——"
            "两者不能混用：纯鼠标事件对 draggable 元素只会触发 dragstart，drop 永远不发生。"
            "安全挑战滑块不走这里，请用 drag 或 visual_drag"
        ),
        category="element",
        properties={
            "source_target_id": {"type": "string", "maxLength": 200},
            "source_locator": LOCATOR_PROPERTY,
            "target_target_id": {"type": "string", "maxLength": 200},
            "target_locator": LOCATOR_PROPERTY,
            "steps": {
                "type": "integer",
                "minimum": 4,
                "maximum": 60,
                "description": "鼠标通道的移动分步数，默认 12",
            },
            "step_delay_ms": {"type": "integer", "minimum": 0, "maximum": 200},
        },
        returns="实际使用的通道、源与目标名称；原生通道会附带拖拽数据的 MIME 类型",
        requires_observation=True,
        idempotent=False,
        counts_as_action=True,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="save_pdf",
        description=(
            "把当前页面导出为 PDF 并写入私有文件，用于留存报表、账单与对账单。"
            "支持纸张、横纵向、缩放、页边距与页码范围"
        ),
        category="page",
        properties={
            "label": {"type": "string", "minLength": 1, "maxLength": 60},
            "paper": {"type": "string", "enum": sorted(PAPER_SIZES)},
            "landscape": {"type": "boolean"},
            "print_background": {"type": "boolean", "description": "默认 true，保留底色与背景图"},
            "scale": {"type": "number", "minimum": 0.1, "maximum": 2},
            "margin_inches": {"type": "number", "minimum": 0, "maximum": 5},
            "page_ranges": {
                "type": "string",
                "maxLength": 100,
                "description": "示例 1-3 或 1,3,5-7；留空导出全部",
            },
            "prefer_css_page_size": {"type": "boolean"},
        },
        returns="PDF 文件路径与字节数",
        requires_observation=False,
        idempotent=True,
        counts_as_action=False,
    ),
    ToolDefinition(
        name="measure_performance",
        description=(
            "采集 Core Web Vitals 四项 LCP、FCP、CLS、INP，导航计时 TTFB、DOM 就绪与 load，"
            "资源概览与运行时计数器，并按 Google 公开阈值给出好/需改进/差评级。"
            "要测到 LCP 必须传 reload=true——采集器只有早于导航安装才能观察到最大内容绘制，"
            "导航之后再装连 buffered 也补不回来"
        ),
        category="performance",
        properties={
            "reload": {
                "type": "boolean",
                "description": "先安装采集器再重载页面，测量一次完整加载；LCP 必需",
            },
            "settle_seconds": {
                "type": "number",
                "minimum": 0,
                "maximum": 30,
                "description": "读取前的稳定等待，默认 0.5 秒",
            },
        },
        returns="各项指标取值、评级、导航计时、最慢的五个资源与 DOM 规模计数器",
        requires_observation=False,
        idempotent=True,
        counts_as_action=False,
    ),
)


EMULATION_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="emulate_environment",
        description=(
            "模拟设备视口、网络与 CPU 速度、语言、时区、地理位置与深浅色偏好。"
            "移动端站点会按 UA 与视口返回完全不同的 DOM，验证移动版必须先切设备。"
            "各维度相互独立，只传要改的那些，其余保持不变；reset 清除全部覆盖。"
            "返回值里的 effective 是页面实际生效的环境，可能与请求值不同"
        ),
        category="emulation",
        properties={
            "device": {
                "type": "string",
                "enum": list(DEVICE_PRESETS),
                "description": "设备预设，同时设置视口、UA、客户端提示与触控",
            },
            "viewport": {
                "type": "object",
                "properties": {
                    "width": {"type": "integer", "minimum": 100, "maximum": 4000},
                    "height": {"type": "integer", "minimum": 100, "maximum": 4000},
                    "device_scale_factor": {"type": "number", "minimum": 0.1, "maximum": 5},
                    "mobile": {"type": "boolean"},
                },
                "required": ["width", "height"],
                "additionalProperties": False,
                "description": "显式视口；与 device 同时给出时尺寸以本字段为准",
            },
            "network_preset": {
                "type": "string",
                "enum": list(NETWORK_PRESETS),
            },
            "network": {
                "type": "object",
                "properties": {
                    "offline": {"type": "boolean"},
                    "latency_ms": {"type": "number", "minimum": 0, "maximum": 60000},
                    "download_kbps": {"type": "number"},
                    "upload_kbps": {"type": "number"},
                },
                "additionalProperties": False,
                "description": "自定义网络条件；吞吐为 -1 表示不限速",
            },
            "cpu_throttle_rate": {
                "type": "number",
                "minimum": 1,
                "maximum": 20,
                "description": "CPU 降速倍率，4 表示比当前机器慢 4 倍",
            },
            "locale": {"type": "string", "maxLength": 35},
            "timezone": {"type": "string", "maxLength": 60},
            "color_scheme": {"type": "string", "enum": list(COLOR_SCHEMES)},
            "geolocation": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number", "minimum": -90, "maximum": 90},
                    "longitude": {"type": "number", "minimum": -180, "maximum": 180},
                    "accuracy": {"type": "number", "minimum": 0},
                },
                "required": ["latitude", "longitude"],
                "additionalProperties": False,
            },
            "reset": {
                "type": "boolean",
                "description": "清除全部模拟覆盖，不能与其他参数同时使用",
            },
        },
        required=(),
        returns="requested 为生效中的模拟设置，effective 为页面回读到的实际环境",
        requires_observation=False,
        idempotent=True,
        counts_as_action=False,
    ),
)

DIALOG_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="handle_dialog",
        description=(
            "设置 alert/confirm/prompt/beforeunload 的应答方式，并查看本次会话已接管的对话框。"
            "对话框会在弹出的瞬间被自动应答，因此本工具设置的是下一次或后续怎么答，"
            "而不是回答当前这一个。默认 confirm 与 prompt 取消、alert 与 beforeunload 确认；"
            "需要确认删除、覆盖这类不可逆操作时，先用 scope=next 把下一次改成 accept。"
            "action 传 inspect 表示只读查看策略与记录"
        ),
        category="dialog",
        properties={
            "action": {
                "type": "string",
                "enum": ["accept", "dismiss", "inspect"],
            },
            "scope": {
                "type": "string",
                "enum": ["next", "session"],
                "description": "next 只影响下一次对话框，session 持续到再次修改；默认 next",
            },
            "dialog_kinds": {
                "type": "array",
                "items": {"type": "string", "enum": list(DIALOG_KINDS)},
                "maxItems": 4,
                "uniqueItems": True,
                "description": "只对指定类型生效；省略表示全部类型",
            },
            "prompt_text": {
                "type": "string",
                "maxLength": 1000,
                "description": (
                    "accept 一个 prompt 时填入的文本；敏感值请改用 prompt_text_input_key"
                ),
            },
            "prompt_text_input_key": {
                "type": "string",
                "maxLength": 100,
                "description": "prompt 填入值对应的任务输入键，值不进入工具参数与轨迹",
            },
        },
        required=("action",),
        returns="生效中的分类型应答策略与已接管对话框记录；prompt 填入值对模型脱敏",
        requires_observation=False,
        idempotent=True,
        counts_as_action=False,
        requires_write_permission=True,
    ),
)

SCRIPT_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="export_action_script",
        description=(
            "把本次会话中已成功执行并通过业务后置校验的页面动作导出成可独立重跑的 Python 脚本。"
            "观察候选的 target_id 带会话版本号、跨会话必然失效，导出时会用当时命中的那个候选"
            "反推出 test_id、css、role+name 或文本定位器；敏感值继续以任务输入键引用，"
            "不写进脚本明文。跑通一个流程后想把它固化成可重复执行的自动化时用本工具"
        ),
        category="script",
        properties={
            "target": {
                "type": "string",
                "enum": list(SCRIPT_TARGETS),
                "description": "脚本形态，默认 python_toolkit",
            },
        },
        required=(),
        returns=(
            "可运行脚本文本、步骤清单、引用到的任务输入键，以及需要人工补定位器的步骤；"
            "模型侧只拿步骤清单与统计，不含脚本正文"
        ),
        requires_observation=False,
        idempotent=True,
        counts_as_action=False,
    ),
)

FRAME_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="list_frames",
        description=(
            "列出当前页面的主框架与全部 iframe，返回可用于定位器 frame_id 的帧标识。"
            "页面里的表单、支付控件或验证码被放进 iframe 时，主框架定位器必然找不到元素，"
            "先用本工具拿到 frame_id 再定位"
        ),
        category="navigation",
        properties={},
        returns=("帧清单：frame_id、父帧、脱敏 URL、名称、嵌套深度以及是否跨站独立进程"),
        counts_as_action=False,
    ),
)

PAGE_CONTROL_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="press_key",
        description=(
            "向当前焦点或指定目标派发功能键与组合键，例如回车提交搜索、Tab 切换焦点、"
            "Escape 关闭弹层、方向键移动选项。普通文本输入必须使用 input_text 系列工具。"
            "expect_kind/expect_value 可省略，缺省按“页面有变化”校验"
        ),
        category="element",
        properties={
            "key": {"type": "string", "enum": list(SUPPORTED_KEY_NAMES)},
            "modifiers": {
                "type": "array",
                "items": {"type": "string", "enum": list(SUPPORTED_MODIFIER_NAMES)},
                "maxItems": 4,
                "uniqueItems": True,
            },
            "repeat": {"type": "integer", "minimum": 1, "maximum": 20},
            "target_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "locator": LOCATOR_PROPERTY,
            "expect_kind": {
                "type": "string",
                "enum": [
                    "url_contains",
                    "title_contains",
                    "text_contains",
                    "fingerprint_changed",
                ],
            },
            "expect_value": {"type": "string"},
        },
        required=("key",),
        returns="按键审计与业务后置条件校验结果；轨迹只记录键名、修饰键掩码和次数",
        idempotent=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="navigate_history",
        description=(
            "在当前标签页执行后退、前进或重新加载；不跨越任务授权范围，没有对应历史记录时确定性失败。"
            "expect_kind/expect_value 可省略，缺省按“页面有变化”校验"
        ),
        category="navigation",
        properties={
            "action": {"type": "string", "enum": ["back", "forward", "reload"]},
            "expect_kind": {
                "type": "string",
                "enum": [
                    "url_contains",
                    "title_contains",
                    "text_contains",
                    "fingerprint_changed",
                ],
            },
            "expect_value": {"type": "string"},
        },
        required=("action",),
        returns="历史导航回执与业务后置条件校验结果，附动作后的新页面观察 page",
        idempotent=False,
    ),
)

TAB_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="list_tabs",
        description=(
            "只读列出当前浏览器的页面标签；返回脱敏 URL、标题、"
            "是否当前页和是否任务自建，不改变页面状态"
        ),
        category="tab",
        properties={},
        returns="标签页列表，含 target_id、脱敏 URL、标题、是否当前页和是否任务自建",
        counts_as_action=False,
    ),
    ToolDefinition(
        name="open_tab",
        description=(
            "在任务授权域名范围内新建标签页并切换过去；新页由任务自有，可以用 close_tab 关闭。"
            "打开后旧页面观察全部失效，必须重新观察当前页面"
        ),
        category="tab",
        properties={"url": {"type": "string", "minLength": 1, "maxLength": 2000}},
        required=("url",),
        returns="新标签页的 target_id 与脱敏 URL；打开成功后必须重新观察当前页面",
        idempotent=False,
    ),
    ToolDefinition(
        name="switch_tab",
        description=(
            "把任务操作切换到 list_tabs 返回的指定标签页；"
            "切换后旧页面观察全部失效，必须重新观察当前页面"
        ),
        category="tab",
        properties={
            "target_id": {"type": "string", "minLength": 1, "maxLength": 80},
        },
        required=("target_id",),
        returns="切换结果；切换成功后必须重新观察当前页面",
    ),
    ToolDefinition(
        name="close_tab",
        description=(
            "关闭任务自己创建的标签页；用户原有页面不能关闭。关闭当前页后会自动回到其余任务页面"
        ),
        category="tab",
        properties={
            "target_id": {"type": "string", "minLength": 1, "maxLength": 80},
        },
        required=("target_id",),
        returns="关闭结果；关闭当前页时会自动切换到其余任务页面",
        idempotent=False,
    ),
)

FILE_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="upload_files",
        description=(
            "把本地文件注入到 file input，用观察候选的 target_id 或显式定位器指定目标；"
            "路径必须是已存在的绝对路径，模型侧请用 path_input_keys 引用任务输入，"
            "不要把密码、令牌一类敏感值放进文件路径参数"
        ),
        category="file",
        properties={
            "target_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "locator": LOCATOR_PROPERTY,
            "paths": {
                "type": "array",
                "description": "本地绝对路径列表；外部脚本可直接传，模型优先用 path_input_keys",
                "items": {"type": "string", "minLength": 1, "maxLength": 1024},
                "minItems": 1,
                "maxItems": 10,
                "uniqueItems": True,
            },
            "path_input_keys": {
                "type": "array",
                "description": "任务输入键列表，键对应的值必须是本地绝对文件路径",
                "items": {"type": "string", "minLength": 1, "maxLength": 100},
                "minItems": 1,
                "maxItems": 10,
                "uniqueItems": True,
            },
            "expect_kind": {
                "type": "string",
                "enum": [
                    "url_contains",
                    "title_contains",
                    "text_contains",
                    "target_exists",
                    "fingerprint_changed",
                ],
            },
            "expect_value": {"type": "string"},
        },
        required=(),
        returns=(
            "上传回执，含已注入文件的名称与字节数；执行层会回读 input.files 校验，可选业务后置条件"
        ),
        idempotent=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="list_downloads",
        description=(
            "列出本任务已捕获的浏览器下载；文件落在任务产物目录的 downloads 子目录，权限为 0600"
        ),
        category="file",
        properties={
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "最多返回多少条，默认 20",
            },
        },
        required=(),
        returns="下载记录列表，含 suggested_filename、path、size、state 与脱敏 URL",
        requires_observation=False,
        idempotent=True,
        counts_as_action=False,
    ),
    ToolDefinition(
        name="wait_for_download",
        description=(
            "等待一次浏览器下载完成；通常先 click 触发下载，再按建议文件名或 URL 子串等待。"
            "下载接管在浏览器启动时已启用，无需另行配置目录"
        ),
        category="file",
        properties={
            "suggested_filename": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "Content-Disposition 或 download 属性给出的建议文件名",
            },
            "url_contains": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "按下载 URL 子串匹配",
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 0.5,
                "maximum": 120,
                "description": "等待上限，默认 30 秒",
            },
        },
        required=(),
        returns="完成后的下载记录，含可读文件路径与原始 GUID 路径",
        requires_observation=False,
        idempotent=True,
        counts_as_action=False,
    ),
)

STORAGE_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="read_cookies",
        description=(
            "读取当前页面或指定 URL 的 Cookie；纯 CDP 协议，headless 与后台标签页均可执行，"
            "不会把页面切到前台。模型侧返回值已脱敏，完整值只回给调用方"
        ),
        category="storage",
        properties={
            "url": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "description": "要读取的页面 URL；省略时使用当前页",
            },
            "names": {
                "type": "array",
                "description": "只返回这些名称的 Cookie；省略时返回全部",
                "items": {"type": "string", "minLength": 1, "maxLength": 256},
                "minItems": 1,
                "maxItems": 50,
                "uniqueItems": True,
            },
        },
        required=(),
        returns="Cookie 列表，含名称、域、路径、过期时间与脱敏后的值",
        requires_observation=False,
        idempotent=True,
        counts_as_action=False,
    ),
    ToolDefinition(
        name="set_cookie",
        description=(
            "通过 Network.setCookie 写入 Cookie；不依赖页面可见或焦点。"
            "敏感值请用 value_input_key 引用任务输入，不要把令牌写进工具参数"
        ),
        category="storage",
        properties={
            "name": {"type": "string", "minLength": 1, "maxLength": 256},
            "value": {
                "type": "string",
                "maxLength": 4096,
                "description": "外部脚本可直接传；模型优先用 value_input_key",
            },
            "value_input_key": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "任务输入键，值必须是 Cookie 字符串",
            },
            "url": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "description": "Cookie 所属页面 URL；省略时使用当前页",
            },
            "path": {"type": "string", "minLength": 1, "maxLength": 1024},
            "domain": {"type": "string", "minLength": 1, "maxLength": 253},
            "http_only": {"type": "boolean"},
            "secure": {"type": "boolean"},
            "expires": {
                "type": "number",
                "description": "Unix 时间戳；省略表示会话 Cookie",
            },
        },
        required=("name",),
        returns="已写入 Cookie 的元数据，不含明文值",
        requires_observation=False,
        idempotent=False,
        counts_as_action=False,
        requires_write_permission=True,
    ),
    ToolDefinition(
        name="read_web_storage",
        description=(
            "读取 localStorage 或 sessionStorage；在指定 frame_id 的 document 上执行固定脚本，"
            "不要求页面在前台。省略 key 时只列出键名，最多 50 个"
        ),
        category="storage",
        properties={
            "storage_kind": {
                "type": "string",
                "enum": ["local", "session"],
                "description": "local 表示 localStorage，session 表示 sessionStorage",
            },
            "key": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": "要读取的键；省略时返回键名列表",
            },
            "frame_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "iframe 的 frame_id，来自 list_frames；省略表示主框架",
            },
        },
        required=("storage_kind",),
        returns="键列表或单个键的值；模型侧值已脱敏",
        requires_observation=False,
        idempotent=True,
        counts_as_action=False,
    ),
    ToolDefinition(
        name="write_web_storage",
        description=(
            "写入或删除 localStorage/sessionStorage 项；纯协议操作，后台标签页可用。"
            "敏感值请用 value_input_key，模型不要把令牌写进 value"
        ),
        category="storage",
        properties={
            "storage_kind": {
                "type": "string",
                "enum": ["local", "session"],
            },
            "key": {"type": "string", "minLength": 1, "maxLength": 256},
            "value": {
                "type": "string",
                "maxLength": 65536,
                "description": "外部脚本可直接传；模型优先用 value_input_key",
            },
            "value_input_key": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
            },
            "frame_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
            },
            "remove": {
                "type": "boolean",
                "description": "为 true 时删除该键，不需要 value",
            },
        },
        required=("storage_kind", "key"),
        returns="写入或删除结果",
        requires_observation=False,
        idempotent=False,
        counts_as_action=False,
        requires_write_permission=True,
    ),
)

CAPABILITY_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="report_capability_gap",
        description=(
            "仅当当前开放的现有工具都无法表达任务所需能力时，产出一条结构化能力缺口记录。"
            "记录经脱敏后原样返回给调用方，由调用方决定是否落库、上报或据此换路；"
            "本工具自身不写任何存储、不生成代码、不修改项目、不重启服务。"
            "网站报错、选择器不确定、缺少任务输入或单次动作失败不属于能力缺口"
        ),
        category="capability",
        properties={
            "area": {"type": "string", "enum": list(CAPABILITY_AREAS)},
            "capability": {
                "type": "string",
                "minLength": 1,
                "maxLength": 600,
                "description": "现有框架缺少的确定性工具能力",
            },
            "evidence": {
                "type": "string",
                "minLength": 1,
                "maxLength": 600,
                "description": ("当前开放工具无法表达该能力的结构化事实，不包含网页正文或凭据"),
            },
            "related_tool": {
                "type": "string",
                "maxLength": 100,
                "description": "最接近该能力的现有工具名",
            },
        },
        required=("area", "capability", "evidence"),
        returns="脱敏缺口记录，返回给调用方自行处置；不写存储、不修改项目、不阻塞任务",
        counts_as_action=False,
    ),
)

# 注册顺序即模型看到的工具顺序，调整顺序会改变下发给模型的 schema 序列。
BROWSER_TOOLS = ToolRegistry(
    (
        *CORE_TOOLS,
        *NETWORK_TOOLS,
        *TRAFFIC_TOOLS,
        *DIAGNOSTIC_TOOLS,
        *LOCATOR_TOOLS,
        *ELEMENT_READ_TOOLS,
        *POINTER_TOOLS,
        *FRAME_TOOLS,
        *PAGE_CONTROL_TOOLS,
        *TAB_TOOLS,
        *FILE_TOOLS,
        *STORAGE_TOOLS,
        *FORM_TOOLS,
        *PAGE_EXTRA_TOOLS,
        *EMULATION_TOOLS,
        *DIALOG_TOOLS,
        *SCRIPT_TOOLS,
        *CAPABILITY_TOOLS,
    )
)


def schemas_of(definitions: tuple[ToolDefinition, ...]) -> tuple[dict[str, Any], ...]:
    """按声明顺序生成分组 schema，供各执行模块保持既有导出名。"""

    return tuple(definition.json_schema() for definition in definitions)


def names_of(definitions: tuple[ToolDefinition, ...]) -> frozenset[str]:
    return frozenset(definition.name for definition in definitions)

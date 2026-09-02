# Witty 浏览器工具库项目状态

- 更新时间：`2026-09-02 11:30 CST`
- 当前阶段：`阶段 2 - 面向外部大模型智能体的纯工具库`
- 项目定位：本仓库是给**外部大模型智能体**调用的确定性浏览器工具库。仓库内不存在任何发起模型请求的代码；由外部智能体（Codex、Claude、Cursor 等）决定下一步，本库保证每一步的正确性——参数校验、业务后置条件、脱敏、非幂等防重放、安全挑战约束、批量采集完整性门全部由固定代码执行。两条接入方式：能执行 Python 的智能体走 `witty_browser_auto.toolkit.launch_browser_toolkit` 并读 `skills/use-browser-toolkit/SKILL.md`；不能执行代码或非 Python 的框架走 `witty-browser-auto mcp` 的 MCP stdio 服务端。使用文档是 `skills/use-browser-toolkit/SKILL.md`（64 个开放工具全部有示例，契约测试强制文档与工具面一致）。
- 状态定义：`已实现`、`部分实现`、`未实现`、`阻塞`、`已移除`

## 架构收敛记录（2026-08-28）

按"面向大模型、让别的智能体快速写出代码"的产品决策，删除了全部大模型调用侧代码，项目从"自带智能体循环的 RPA"收敛为纯工具库。被删代码完整备份在仓库外 仓库外的独立备份目录 `2026-08-28-model-removal/`（按原相对路径存放，可整目录还原）：

- 模型网关与循环：`model/`、`agent/engine*` 及全部提示词/工具选择/监督/修复模块、`application.py`
- 对话产品层：`workbench/`、`config_ui/`、`desktop/`（Electron + Pi sidecar）、`pi_runtime.py`
- 自我进化：`evolution/`、`runtime/patch_repair.py` 及循环状态存储
- 配置与领域模型同步去模型化：`ModelConfig`、`ModelGateway`、`model_profiles` 等全部移除

import 层面已验证：加载 `witty_browser_auto.toolkit` 的完整闭包不含任何被删模块。

## 当前能力

| 编号 | 能力 | 状态 | 证据或说明 |
| --- | --- | --- | --- |
| T-001 | 自研 CDP 内核 | 已实现 | `cdp/`：WebSocket 命令关联、事件路由、超时、断线清理、Target Session 生命周期；运行依赖仅 `aiohttp` |
| T-002 | 浏览器生命周期 | 已实现 | 受管可见 Chrome、按作用域隔离的持久 profile、回环 CDP 端口；`takeover` 模式经 Chrome 原生远程调试接管日常浏览器，不启动新进程 |
| T-003 | 页面观察与定位 | 已实现 | AX+DOM 候选、六类显式定位器（唯一性与稳定/可见/命中校验）、iframe 帧作用域（同站 isolated world、跨站 OOPIF 二次接管）、Shadow DOM 穿透 |
| T-004 | 页面操作工具族 | 已实现 | 点击（左/右/双击）、输入、选择、悬停、滚动、按键组合、表单批量填写（逐字段回读校验）、元素双通道拖放（HTML5/指针自动择路）、页面历史（含 bfcache）、原生对话框接管、环境模拟（设备/网络/时区/地理/深浅色） |
| T-005 | 结构化采集 | 已实现 | 只读结构分析 + 确定性整页采集：页码/下一页/加载更多/无限滚动/逐条详情；完整性门要求声明总数闭合或稳定终点，弱证据一律不判成功；产物 0600 JSON/CSV。真实订单页曾闭合 9 页 87/87 条详情 |
| T-006 | 已验证采集程序库 | 已实现 | 成功规格经存储前验证门（重进入口 + 结构探针）晋升，按 `作用域+场景+路径模板+结构指纹` 落 SQLite；`replay_collection_program` 零决策重放，失配降权回退并返回明确原因；任务输入值禁止固化，连续失败自动禁用。**本轮已从引擎终态挪到工具库路径：`run_structured_extraction` 成功即自动晋升，重放成为外部可调用工具** |
| T-007 | 网络流量检查 | 已实现 | 全部资源类型的请求/响应头（合并 extraInfo）、六段 timing、initiator、重定向链、TLS 与证书详情（`securityDetails` 归一化，SAN 有界）、WebSocket 帧、SSE 消息（`eventSourceMessageReceived` 逐条记录，长连接不关闭也能读）；正文/头/帧/SSE 全文搜索定位来源；超过内存上限的大响应落 0600 私有文件（单体 64 MiB、全局 256 MiB 双上限），`read_network_body` 返回路径；HAR 1.2 导出含 `_websockets`、`_serverSentEvents`、`_securityDetails`；页面会话内请求重放（复用 Cookie/TLS，逐跳头过滤，Host 重写 authority） |
| T-008 | 接口逆向与代码导出 | 已实现 | `analyze_api_endpoint` 归纳 URL 模板/参数分类/鉴权位置/schema/record_path/分页策略；`export_request_code` 导出 curl/requests/httpx/fetch/axios，凭据走环境变量占位；导出代码在真实服务端跑通过 |
| T-009 | 主动分页采集 | 已实现 | `collect_api_pages` 沿 page_number/offset/cursor 遍历，起点取样本自身取值，只改分页参数；闭合判据只认正面证据，识破服务端忽略分页参数；缺口条数明确报出。分页字段可在 query 或 POST 的 JSON/表单请求体（`page_in=body`，嵌套字段按点号路径、保留原字段类型）；游标可来自响应正文、自定义响应头（`cursor_in=header`）或 `Link: rel=next`（`cursor_in=link`，下一页 URL 采信服务端） |
| T-010 | 会话与文件 | 已实现 | Cookie/Web Storage 受控读写、Playwright 兼容会话态整体导出导入（0600）、文件上传（触碰浏览器前校验）、下载接管（GUID 落盘 + 可读副本） |
| T-011 | 动作脚本导出 | 已实现 | `export_action_script` 把已验证动作序列导出为可独立重跑的 Python 脚本，会话内 target_id 在登记时反推为六档稳定定位器；独立子进程真实重跑验收 |
| T-012 | 诊断与性能 | 已实现 | CDP 页面诊断（控制台/异常/失败请求有界摘要）、`measure_performance`（LCP 需 reload 的口径明示）、元素与区域截图、PDF 导出 |
| T-013 | 隐私纪律与生产保护 | 已实现 | 敏感值经 `inputs` 键名引用、URL/日志/事件全链路脱敏、业务数据只落私有 0600 文件；`model_data` 有界视图与完整调用方视图分流；`security.read_only` / `WITTY_BROWSER_AUTO_READ_ONLY` / `read_only=True` 在执行前硬禁副作用工具 |
| T-014 | 工具契约单一事实源 | 已实现 | `toolkit/catalog.py` 声明全部 68 个工具（64 个开放 + 4 个保留终态语义定义)；registry 派生 schema 与执行前校验。SKILL.md 已按渐进披露重写：主文档（触发描述、纪律、选路决策、工具速查、高频示例）+ 4 个 `references/` 深指南，契约测试对主文档与参考文件全量做签名绑定、500 行预算与双向指引校验 |
| T-015 | URL 记忆 | 部分实现 | SQLite 按项目/租户/账号/场景隔离，后台异步读写；采集程序库在用；记忆版本、显式失效、加密和保留策略待补 |
| T-016 | Skills/MCP 扩展 | 部分实现 | `extensions/` 保留 stdio MCP 客户端与 Skills 加载（无模型依赖）；当前 toolkit 装配默认不启用，需调用方显式注入 |
| T-017 | 外部框架消费接口 | 已实现 | `toolkit/serialization.py` 承担观察与工具结果通往模型的唯一出口：`observation_to_dict`/`observation_to_prompt` 输出 JSON 安全结构与紧凑文本（候选按置信度截断到 24 个、逐字列出 target_id、显式标注截断），`tool_result_to_dict` 分调用方/模型两路视图并保留 `failure_kind` 与后置条件结论；`BrowserToolkit.observe_for_model` 一步到位。`tool_schemas()`/`describe_tools()` 默认只给 64 个可外部调用工具，终态工具需显式取用；顶层 `witty_browser_auto` 直接导出 schema、序列化函数与异常类型 |
| T-018 | MCP stdio 服务端 | 已实现 | `mcp_server/`：标准库实现的换行分隔 JSON-RPC（协议 `2025-06-18`）、`initialize`/`tools/list`/`tools/call`/`ping`；`core`/`all` 两个工具档位加分类与追加过滤，避免 64 个 schema 撑爆客户端上下文；MCP 特有的 `open_browser`/`observe`/`close_browser` 三个会话工具；协议问题回 JSON-RPC error 而工具失败回 `isError`，连接不中断；`witty-browser-auto mcp` 子命令支持 origin 授权与经文件注入的敏感输入，日志固定走 stderr 以免破坏 stdout 分帧。未实现 resources/prompts/sampling 与 Streamable HTTP |
| T-019 | 多模态标注截图 | 已实现 | `browser/annotation.py` + `capture_annotated_screenshot`：把观察候选按置信度编号画在视口截图上，图例将编号对回 `target_id`，解决"模型在图上看见按钮但不知道该用哪个 target_id"。覆盖层 `pointer-events:none`、不碰业务节点、不滚动页面、`finally` 必除；图例只保留真正画上去的编号，视口外候选不入图例；模型视图不含截图本机路径 |
| T-020 | 页面正文 Markdown 与链接清单 | 已实现 | `browser/page_content.py` + `read_page_markdown`/`list_page_links`：主内容容器自动判定（语义标签优先，退化到文本量减链接密度惩罚），剥离导航/页眉页脚/侧栏/隐藏节点与表单控件，保留标题层级、嵌套列表、代码块、GFM 表格与绝对地址行内链接；默认 40000 字符上限，截断时仍如实报告页面真实总长。Markdown 进模型视图（这是它存在的意义），但重复结构化记录仍必须走结构化采集。`list_page_links` 给出绝对地址、去重、同源与子串过滤，作为调用方自行编排站内遍历的起点；本库不做整站爬取、不读 robots.txt、不做全局限速。转换保真度由真实 Chrome 集成测试证明 |
| T-021 | 抓取策略与限速 | 已实现 | `network/robots.py` 自实现 robots.txt 解析与匹配（`*` 通配、`$` 锚定、最长优先、同长 Allow 胜出；4xx 放行、5xx 与取不到判状态未知而非放行）；`browser/pacing.py` 按主机最小间隔限速，生效值取站点声明与调用方配置的较大者。`check_crawl_policy` 给出判定、Crawl-delay 与 Sitemap。**默认纯咨询**——robots.txt 约束抓取，而本库也用于交互场景，默认拦截会挡掉正当用途；装配时打开 `respect_robots` 后 `navigate`/`open_tab` 按判定硬拦并自动限速。判据由真实 Chrome 集成测试按“服务端收到过哪些路径”验证 |
| P3-001 | 窗口自动化驱动 | 未实现 | 只有 capability 协议边界 |
| R-001 | AI 智能体运行时（引擎循环、多模型协作、监督纠正） | 已移除 | 转纯工具库；代码在仓库外备份 |
| R-002 | 聊天式任务工作台与 Electron 桌面壳 | 已移除 | 同上 |
| R-003 | 评测驱动自我进化与模型补丁链 | 已移除 | 同上 |
| R-004 | 本机配置中心 UI | 已移除 | 配置收敛为 `.witty-browser-auto/config.json` + 环境变量；`witty-browser-auto doctor` 自检浏览器与存储 |

## 本轮已完成（2026-08-28 架构收敛）

- **删除全部模型调用代码**：`model/`、引擎循环 16 个模块、`workbench/`、`config_ui/`、`desktop/`、`evolution/`、`pi_runtime.py`、`application.py`、`runtime/` 循环状态存储全部移出仓库（外部备份可还原）。`agent/` 收敛为纯工具执行层，`agent/__init__.py` 不再有拉起引擎的包副作用——此前 `import witty_browser_auto.toolkit` 会在 import 时加载整个模型栈，现已验证闭包干净。
- **配置与领域模型去模型化**：删除 `ModelConfig`、全部 `WITTY_BROWSER_AUTO_MODEL_*` 环境变量、`ModelGateway` 协议、`TaskSpec.model_profiles`；`RuntimeConfig` 只留 `log_level`。CLI 收敛为 `version` + `doctor`（浏览器/存储自检，无模型检查）。
- **采集程序晋升与重放接到工具库**（原引擎侧 P0 能力换宿主）：`run_structured_extraction` 成功且强证据时自动过存储前验证门晋升；新增开放工具 `replay_collection_program`——查程序库、入口探针、通过即整页采集，失配返回 `fallback` 指引并降权。外部智能体的代码模式变为"先试重放，失配再检查+提交规格"。开放工具 57 → 58。
- **双轨动作资产与只读硬门控**：执行器统一记录页面、表单和元素拖放动作；导出时把嵌套 `target_id` 解析为稳定定位器。新增部署级 `read_only` 策略，Python/MCP/环境变量三条入口一致拦截点击、输入、上传、存储写入和请求重放，策略拒绝发生在浏览器调用之前。
- **文档面向外部智能体重写**：README 改为工具库快速上手；SKILL.md 新增采集程序重放一节并更新工具数量（契约测试锁定）。
- 验证：全量 `602 passed, 39 skipped`（跳过项均为环境开关控制的真实浏览器测试）；本轮触及文件 ruff check/format 通过（存量 UP042/ASYNC240 不变）；compileall 通过；`check_project_state.py` 通过；import 闭包冒烟检查确认无模型模块加载。

## 验证证据（保留能力的关键基线）

- 采集程序三跑回归（`tests/test_collection_program.py`，8 项）：首跑经 ToolExecutor 提交规格并过门晋升；二跑 `replay_collection_program` 命中并成功（断言 `replay=True`、计数 80、程序增信）；三跑行选择器失配，探针拒绝、程序降权、返回 fallback 指引。
- 真实浏览器集成套件保留 39 项（`WITTY_BROWSER_AUTO_RUN_BROWSER_TESTS=1` 时执行）：指针/iframe/表单/会话态/对话框/模拟/拖放/PDF/性能/流量/WebSocket/接口代码导出/分页采集/动作脚本重跑。
- 历史真实站点基线：订单页 9 页 87/87 条详情闭合、Excel 回读与 CSV 一致；分页采集 E2E 判据取"服务端实际被请求过哪几页"；导出代码在鉴权服务端真实跑通 `total == 87`。
- 运行依赖仍只有 `aiohttp`。

## 当前未验收与已知边界

- 唯一使用方式是 Python API（`import witty_browser_auto.toolkit`）；`witty_browser_auto run`/`chat` 已随引擎与工作台移除。
- 采集程序重放的真实站点命中率未验收；探针只验证入口页结构，翻页中途改版由重放时的完整性门拦下（表现为失败降权而非提前识破）。
- 浏览器崩溃重连、Target 重建、通用分类退避、OCR 未实现；`observe()` 语义候选仍只覆盖主框架。
- Chrome 本体崩溃后的恢复、多用户调度不在纯工具库范围内，由调用方自行管理会话生命周期。
- 截图像素级遮罩、工件加密和自动过期未实现。
- 抓取策略的已知边界：robots.txt 的 User-agent 匹配用包含关系而非严格 token 解析；`crawl_agent` 默认 `WittyBrowserAuto` 但浏览器实际发出的仍是 Chrome UA，判定名与实际 UA 不一致；判定按 origin 缓存且不过期（需要时传 `refresh=True`）；限速只覆盖 `navigate` 与 `open_tab`，页面自身资源请求与 `replay_network_request`/`collect_api_pages` 不受约束；只支持 `Crawl-delay`，未实现 `Request-rate`/`Visit-time`；Sitemap 只原样返回、不抓取解析。
- 对标 Firecrawl v2 后明确评估但未实现的能力：整站 `crawl`/`map` 编排（属调用方职责，且需节奏与合规设计）；LLM schema 抽取（本库不调模型，等价能力由调用方模型加确定性采集承担）；PDF/DOCX 解析（需引入解析依赖，与运行依赖只有 aiohttp 冲突）；托管代理轮换、stealth、location 与结果缓存（服务级能力，本地库不对应）；整站编排层面的全局配额与断点续爬。
- 主内容 Markdown 的判定是启发式的：样板与正文混在同一容器时可能带噪或误剥，可用 `selector` 显式指定；表单控件不入 Markdown（读取值仍用 `read_element`）；代码块不识别语言标注；表格跨行跨列会被拉平；链接扫描硬上限 500 条。
- 对标 skills.sh 头部浏览器 skill（vercel-labs/agent-browser）后明确评估但未实现的能力：axe-core 无障碍审计（需内置约 500 KB 第三方 JS，自研部分实现比不做更有害，待决定是否 vendoring）；`read <url>` 的 markdown/llms.txt 文档抓取（与“不生成独立 HTTP 请求”的既有硬边界冲突）；React 组件树与渲染剖析（需启动时注入 react-devtools 钩子，且框架特定）；视频录制；代理与 CA 证书配置；`--allowed-domains` 级别的页面发起流量 containment 与 WebRTC 阻断（真实安全深度缺口，需 Fetch 层白名单加 worker 守卫）；MCP 多会话隔离。
- 死代码待清理（不影响行为，但会误导阅读者）：`VerifiedPlan`/`verified_plans` 表的写入链已断（`save_plan_later`/`best_plan_cached` 无调用方），`ToolExecutionResult.plan_step` 仍被填充但无消费方，`ModelResponse`/`ModelStreamEvent`/`DecisionKind` 已无任何引用，`capability_gap_reported` 写入后从不读取，`extensions/`（Skills/MCP 客户端）保留但不被默认装配。
- 仅约 6 个工具声明了 `model_data` 有界视图，其余工具经 `tool_result_to_dict(for_model=True)` 会回退到完整调用方数据（已用 `data_is_caller_view` 标记）；逐个补齐有界视图是后续工作。
- `BrowserToolkit` 与驱动只保证单事件循环内使用：`execute` 有动作锁，`observe()` 不在锁内，两个 toolkit 共享同一 driver 时各自的观察缓存会分裂，并发调用需调用方自行串行化。
- 抓包仍有的缺口：**Service Worker 自身发起的请求不可见**（需 attach 到 SW target 并在其上启用 Network 域，该行为只能用真实带 SW 的站点验证，未做盲实现）；请求体分页只支持 JSON 对象与表单体（multipart/二进制未支持），header 游标仍需 `page_param` 指明送回哪个查询参数、游标送回请求体的情形未覆盖；大响应落盘仍需先整体读进内存再写盘（流式落盘需 `Network.streamResourceContent`），产物目录无自动过期；HTTP/2 复用连接的后续请求可能缺 `securityDetails`。基于 CDP 只能抓受管浏览器自身流量，跨进程/跨 App 抓包属 mitmproxy 侧车范围。

## 下一阶段入口

1. 用真实站点验收 `replay_collection_program` 命中率与失配回退表现。
2. 按 `docs/research/VERIFIED_PROGRAM_REPLAY_DESIGN.md` P1 把动作序列型状态机程序纳入同一晋升门（依托 `export_action_script` 的定位器反推）。
3. 浏览器断线重连与 Target 重建。
4. 把 `extensions/`（Skills/MCP 客户端）接入 toolkit 默认装配或明确移除。
5. 清理已确认的死代码（`VerifiedPlan` 写入链、`plan_step`、`ModelResponse`/`ModelStreamEvent`/`DecisionKind`、`capability_gap_reported`）。

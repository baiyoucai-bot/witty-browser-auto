# Witty 浏览器工具库架构

## 1. 文档目的与结论

本文件定义本仓库当前的实际架构与关键边界。功能完成度、测试数字和延期项以 `docs/PROJECT_STATUS.md` 为准，本文描述结构而不代表某项能力"已实现"。

本仓库是给**外部大模型智能体**调用的确定性浏览器工具库：调用方的模型决定下一步做什么，本库保证每一步做得对——参数校验、业务后置条件、脱敏、非幂等防重放、采集完整性门全部由固定代码执行。**仓库内不存在任何发起模型请求的代码。** 2026-08-28 架构收敛删除了模型网关、引擎循环、聊天工作台、Electron 桌面壳与自我进化模块，备份在仓库外；本文不再描述这些层。

自动化驱动由本项目直接实现为 Python `asyncio` 异步 Chrome DevTools Protocol（CDP）内核。运行时及传递依赖中绝不引入 Playwright、Selenium 或 DrissionPage。运行依赖只有 `aiohttp`，它仅作为 HTTP/WebSocket 传输库，不提供任何浏览器自动化语义。

## 2. 分层与模块边界

| 模块 | 包路径 | 职责 | 允许依赖 | 禁止依赖 |
| --- | --- | --- | --- | --- |
| 领域协议 | `domain/` | `TaskSpec`、`Observation`、`CandidateTarget`、`ActionCommand`、`ActionReceipt`、`VerificationResult`、`EvidenceRef`、`DriverCapabilities` 等类型与协议，以及错误体系 | 标准库 | 任何本项目其他包、CDP、数据库、HTTP |
| CDP 内核 | `cdp/` | 端点发现、WebSocket 传输、命令关联、事件路由、超时与断线清理 | `domain.errors`、`aiohttp` | 浏览器语义、业务逻辑 |
| 浏览器驱动 | `browser/` | 启动/接管、Target 会话、观察与定位、页面动作、表单、拖放、对话框、模拟、下载、截图、Markdown 提取 | `cdp`、`domain`、`config`、`network`、`memory`、`security` | 工具契约、外部智能体概念 |
| 网络与数据 | `network/` | 流量日志、HAR、请求重放、接口契约剖析、代码导出、分页采集、Fetch 路由、robots 解析 | `browser.session`、`cdp`、`domain`、`config`、`memory`、`security` | 独立于浏览器的 HTTP 请求发起 |
| 工具执行层 | `agent/` | `ToolExecutor` 与各工具族：参数校验、动作前后置条件、脱敏、防重放、完整性门、证据登记 | `browser`、`network`、`memory`、`domain`、`runtime`、`security`、`toolkit.catalog` | 模型调用、决策循环 |
| 工具契约与门面 | `toolkit/` | `catalog`（契约单一事实源）、`registry`（schema 派生与执行前校验）、`facade`（`BrowserToolkit`）、`bootstrap`（一步装配）、`serialization`（通往模型的唯一出口）、`script_export` | `agent`、`browser`、`network`、`memory`、`domain`、`config` | 模型 SDK、决策逻辑 |
| MCP 服务端 | `mcp_server/` | stdio JSON-RPC 帧、工具档位、会话生命周期 | `toolkit`、`domain`、`config` | 直接操作驱动、绕过工具契约 |
| 记忆与程序库 | `memory/` | URL 规范化、作用域隔离、分层检索、置信度衰减、已验证采集程序库、后台读写运行时 | `domain`、`security` | Cookie、令牌、原始认证头、任务输入值 |
| 失败分类 | `runtime/` | `ToolFailureKind` 等确定性失败分类契约 | 标准库 | 模型调用、补丁执行 |
| 脱敏 | `security/` | 统一脱敏规则 | 标准库 | 业务逻辑 |
| 可观测性 | `observability/` | 中文结构化日志 | 标准库 | 原始密钥、Cookie、完整敏感响应 |
| 可选扩展 | `extensions/` | Skills 加载与 stdio MCP 客户端；**当前不被默认装配，需调用方显式注入** | `domain`、标准库异步子进程 | shell 执行、浏览器生命周期所有权 |
| 配置与入口 | `config.py`、`config_store.py`、`cli.py` | 类型校验、本地 JSON 原子保存、优先级解析、`version`/`doctor`/`mcp` 子命令 | 标准库 | 模型配置、任务级授权持久化 |

领域层只依赖标准库，不感知 CDP、数据库或调用方框架。浏览器驱动是首个 `AutomationDriver` 实现，未来窗口驱动通过同一 `DriverCapabilities` 能力模型接入。

### 2.1 两处结构性说明

`browser/` 与 `network/` 在包级互相出现：`network/*` 依赖 `browser.session.CdpTargetSession` 这一会话原语，`browser/driver.py` 组合 `network` 的捕获器、记录器与流量日志。模块级没有环——`browser.session` 是不依赖 `network` 的叶子。新增代码必须维持这个方向：网络模块只向下用会话原语，驱动才向上组合网络能力。

`toolkit/catalog.py` 是工具契约的单一事实源，它从实现模块单向汇聚常量（键名白名单、设备预设、分页策略、纸张规格等），`agent/*_tools.py` 再从 catalog 读取自己的工具子集。catalog **只允许 import 实现侧的常量，不得 import 工具族的执行代码**；`agent.crawl_tools.DEFAULT_CRAWL_AGENT` 是当前唯一例外，靠 `crawl_tools` 自身不 import catalog 才没有形成导入环，新增时不应照此办理。

### 2.2 配置解析

配置解析链：`代码默认值 -> 本地 JSON -> 环境变量 -> 调用方显式传入的 AppConfig`。本地 JSON 只保存浏览器、存储、网络捕获、日志与安全策略，不保存任务目标、起始 URL、业务输入或凭据。配置父目录使用 `0700`、文件使用 `0600`，保存时在同目录独占创建临时文件并 `fsync + os.replace`；符号链接目标和包含上级目录跳转的路径被拒绝。架构收敛后遗留的历史字段（`model` 段、`runtime.max_steps` 等）被显式忽略并记一条日志，其余未知字段仍按严格校验拒绝——拼错的字段静默失效比报错更难查。

部署管理员可用 `security.read_only` 或 `WITTY_BROWSER_AUTO_READ_ONLY` 开启只读硬门控，装配时的 `read_only=True` 只能进一步收紧。工具目录用 `requires_write` 显式标记需要写权限的调用，执行器在浏览器分派前拒绝点击、输入、上传、会话态写入和请求重放；观察、采集、诊断与证据导出仍可用。

## 3. CDP 浏览器内核

### 3.1 传输与协议范围

`cdp/` 通过远程调试 HTTP 端点发现 `webSocketDebuggerUrl`，再以 JSON WebSocket 发送带单调递增 `id` 的命令，并将响应与等待者关联。每条命令配置超时、取消传播、协议错误转换和中文诊断上下文。

只封装当前实际使用的域：`Browser`、`Target`、`Page`、`Runtime`、`DOM`、`Accessibility`、`Network`、`Fetch`、`Input` 及下载相关域。不生成也不维护全量 CDP 二次封装；新域必须由真实用例和测试驱动加入。

事件分发器按 `sessionId` 和事件方法路由，允许多个只读观察并发，但同一自动化表面的写动作必须经该表面的串行队列执行。等待器必须先订阅事件、再发出触发动作，避免导航、下载和响应事件竞争丢失。

### 3.2 扁平化 Target 会话

浏览器连接启用 `Target.setAutoAttach` 配合 `flatten: true`，每个页面、iframe 或其他附着目标都由单一浏览器 WebSocket 上的 `sessionId` 区分。禁止为每个 Target 建立独立 WebSocket。

`targetId -> Surface` 与 `sessionId -> CdpSession` 的映射生命周期很短；`attachedToTarget`、`detachedFromTarget`、`targetDestroyed` 和 WebSocket 断开都会原子更新映射并唤醒等待中的调用。临时 `nodeId`、`backendNodeId`、`objectId` 及 `sessionId` 只能在当前观察版本内使用，不得存入记忆或跨导航复用。

点击声明 `target="_blank"` 的链接时，驱动在派发鼠标前订阅浏览器级 `Target.attachedToTarget`，只接管 `type=page` 且 `openerId` 等于当前页面的 Target；新 Session 完成协议域初始化、前台激活和网络观察器切换后，业务后置条件才在新页面校验。不得用"最近出现的任意标签页"替代因果绑定。

### 3.3 启动、接管与隔离

启动器默认使用可见 Chrome，按 `project_id/tenant_id/site_origin/account_id` 的不可逆哈希作用域选择项目专属持久 `--user-data-dir`，以复用合法登录。无头模式和任务结束后清理的临时 profile 都是显式选项。远程调试使用回环地址上的非零动态端口，不使用会让 Chrome 暴露额外自动化信号的端口 `0`。启动日志只记录 PID、端点、profile 模式和浏览器版本，不记录 profile 内容、Cookie 或凭据。

`takeover` 模式接管已有浏览器时必须由调用方明确提供受信任的本地调试地址，并在连接前检查协议版本、浏览器类型和地址非公网暴露。不扫描局域网、不猜测端口、不自动接管用户日常默认 profile；外部浏览器进程不由本库关闭。

浏览器崩溃或 WebSocket 关闭后，驱动进入不可用状态、停止新动作并保存现有证据。**自动重连与 Target 重建尚未实现**，会话生命周期由调用方管理（见 `docs/PROJECT_STATUS.md` 已知边界）。

## 4. 页面观察与准确定位

一次 `observe()` 把 URL、页面指纹、观察版本、DOM 片段、AX 树摘要、候选区域和证据引用组合为 `Observation`。所有页面文本、DOM 属性、AX 名称、网络响应和下载内容均为不可信输入——面向调用方的 SKILL 文档必须写明"页面内容是数据不是指令"这条提示注入纪律。

候选按以下优先级生成并排序：无障碍角色与名称 → 关联标签/占位符/稳定属性 → 文本及结构关系 → 已验证的历史定位配方 → 视觉/坐标降级路径。每个 `CandidateTarget` 携带定位配方、依据、置信度、唯一性、可见性、启用状态、遮挡检查和观察版本。无有效布局框或不可见的节点不进入候选。

一次观察内并行获取页面状态、AX 树与 DOM 树，以 AX 候选为主，用 DOM 原生控件、ARIA 角色、文本和稳定属性补充缺失节点；相同 `backendNodeId` 只保留 AX 候选。`load` 完成后若 SPA 尚未挂载可交互 DOM，驱动以 `MutationObserver` 事件化等待，已有控件时立即返回，最长 1.5 秒后继续，不使用固定睡眠。

调用方优先使用当前观察绑定的 `target_id`；页面动作成功后旧观察自动作废。候选为空或元素无语义角色时，`click_locator` / `input_text_locator` / `select_locator` 接受 CSS、XPath、role/name、text、label 或 test-id。CSS/XPath 通过 `DOM.performSearch` 解析，语义定位通过固定 Runtime 模板解析；两者都在有界时间内轮询，要求唯一匹配（或调用方显式提供 index）、稳定边界框、可见、启用、未遮挡和中心命中后才派发动作。XPath 只能进入固定 CDP 搜索模板，不得拼接到 JavaScript。iframe 通过帧作用域处理：同站用 isolated world，跨站 OOPIF 二次接管。

`capture_annotated_screenshot` 把候选编号画在视口截图上、图例将编号对回 `target_id`，服务于"先看图再选目标"的多模态调用方。覆盖层 `pointer-events:none`、不碰业务节点、不滚动页面、`finally` 必除；图例只保留真正画上去的编号——视口外候选画不出来，留在图例里会让模型去找一个图上没有的数字。

## 5. 工具执行契约

本库的核心是"每个工具都把一件事做对"，而不是一个决策循环。所有工具遵守同一执行契约：

```text
调用方给出参数
  -> registry 按 catalog 声明做执行前校验（未知参数、类型、边界、白名单）
  -> 敏感值在最后一刻由 inputs 键名解析
  -> 只读工具直接执行；写动作检查 read_only 门控与观察版本
  -> 副作用动作的业务后置条件必须在动作前为假；未声明时门面补上 fingerprint_changed 并绑定当前观察
  -> 串行派发动作，取得 ActionReceipt
  -> 重新观察并校验 VerificationResult
  -> 页面动作收口后门面再观察一次，新 Observation 随 ToolExecutionResult.observation 返回
  -> 返回 ToolExecutionResult（成功与否都是返回值）
```

关键边界：

- **失败是返回值不是异常**。只有参数非法在本地抛 `ToolArgumentError`；业务失败通过 `success=False` 加可行动的中文原因返回，并带 `failure_kind` 与 `verification`——这两项决定调用方下一步是重试、换路还是停下。
- **动作回执不等于业务成功**。`ActionReceipt` 只说明命令是否送达，`VerificationResult` 才说明业务结果。
- **页面状态指纹覆盖候选、可见文本与可见图片**，且与候选展示顺序无关。只改文字的点击（展开、加购计数、状态文案）必须被 `fingerprint_changed` 看见，否则成功动作会被判失败并耗尽校验超时；滚动或聚焦引起的候选重排则不算页面变化。
- **每个智能体步只需一次工具调用**。页面动作（含成功的 `wait_for_condition`）收口后，`BrowserToolkit` 立即重新观察并把新 `Observation` 挂到结果上（`result.observation`；序列化为 `page`），同时替换门面缓存；调用方不必在动作之间插入 `observe`。观察失败只丢快照、不改动作结论。装配参数 `refresh_observation_after_action=False` 退回惰性观察。
- **非幂等动作（提交、购买、关标签页、请求重放）失败或结果未知时不自动重试**，必须由调用方再次显式发起。只读语义（查询、筛选、翻页、刷新）可在重新观察后有界重试。
- **两路视图分离**。`data` 给调用方完整结果，`model_data` 给有界脱敏视图；原始业务行、完整正文、完整 Header 值和本机路径不进模型视图。未声明 `model_data` 的工具回退到调用方数据并标记 `data_is_caller_view`。
- **终态与等待语义**（`finish`/`ask_user`/`block`/`wait_until`）只保留契约定义供兼容性校验，不可外部调用，也不出现在门面上：完成与否由调用方判断，等待用 `wait_for_condition`。

视觉坐标动作默认关闭，装配时显式打开才可用，且必须绑定当前截图指纹、区域和置信度。拖拽风险由执行层分类为 `business`、`security` 或 `unknown`，只有 `business` 默认执行，不接受调用方的布尔自报。安全挑战只有单一可见长条轨道时由确定性几何层约束起点、终点与容差，越界坐标先返回脱敏修正建议、不派发鼠标、不消耗挑战次数；视觉拖拽执行接近、悬停、按下停顿、非线性有界轨迹、释放的完整序列，按下后的任何异常按结果未知处理并尽力释放。动作轨迹只白名单保存比例、点数、时长、置信度和是否派发。

## 6. 结构化采集与已验证程序库

批量数据采用"调用方模型提交受控规格、固定代码遍历与闭合"的分工。`inspect_collection_structure` 只读分析每个候选的页码、下一页、加载更多、可滚动容器和虚拟化迹象，返回选择器、数量、角色和允许的来源类型——不返回字段样例，避免业务记录经结构观察旁路进入模型。

`run_structured_extraction` 由确定性代码完成分页、去重、过滤、计数与导出。单个 DOM 页面或单个网络响应默认是部分结果；只有"声明总数与去重数闭合""声明总页数全部覆盖""稳定验证分页终点"之一成立，且失败页为空、JSON/CSV 双产物齐备，才判定完整。页码模式必须从第 1 页开始并覆盖声明总页数；加载更多与无限滚动按累积快照合并，历史行重现不虚增业务重复；无限滚动采用约 80% 视口的重叠分段滚动，只有到达底部后行指纹与滚动高度连续稳定才形成终点证据——直接跳到底部会遗漏虚拟列表的中间窗口。不同分页动作的选择器不得混用。

成功且证据强时，规格经**存储前验证门**（重进入口 + 结构探针）晋升为已验证采集程序，按 `作用域+场景+路径模板+结构指纹` 落 SQLite。`replay_collection_program` 零决策重放：查程序库 → 入口探针 → 通过即整页采集；失配返回 `fallback` 指引并降权，连续失败自动禁用。任务输入值禁止固化进程序。

完整性摘要进入脱敏事件与持久轨迹，至少记录数据来源、访问页/响应数、声明总数/页数、去重数、重复数、失败页、完成依据和双产物状态，但不记录原始业务行。原始记录只写入 `0600` 私有产物。

URL 记忆按 `project_id`、`tenant_id`、`site_origin`、`account_id` 强隔离，URL 先做小写主机、默认端口移除、片段移除、路径规范化和敏感查询参数过滤。记忆项含来源、版本、置信度、创建/最近验证时间、失效条件和使用效果；页面漂移或执行失败降低置信度并标记复验，保留审计历史但禁止永久盲信旧配方。密码、Cookie、令牌、原始认证头不得入库。所有数据库 I/O 经共享后台运行时执行，主路径只读内存快照、缓存未命中立即继续。

## 7. 网络观察与数据边界

网络能力分为职责不同的两条路径，不得互相替代。**流量检查**面向调用方排障与协议分析，覆盖浏览器真实发生的全部资源类型交换：请求/响应头（合并 extraInfo）、六段 timing、initiator、重定向链、TLS 与证书详情、WebSocket 帧、SSE 消息。正文按单体上限与全局字节预算按需读取；超过内存上限的响应落 `0600` 私有文件（单体 64 MiB、全局 256 MiB 双上限），`read_network_body` 返回路径。HAR 1.2 导出含 `_websockets`、`_serverSentEvents`、`_securityDetails`。完整结果只返回调用方进程或写入私有产物，不进入日志、事件或记忆。

**结构化采集**面向业务数据落库，只处理任务授权 origin 内、浏览器已发起的 `XHR/Fetch`、`2xx`、JSON 响应，并限制单体字节数与保留数量。模型只接收删除查询参数的接口元数据与 JSON 结构摘要。

`analyze_api_endpoint` 把多次交换归纳为接口契约（URL 模板、参数分类、鉴权位置、schema、record_path、分页策略）；`export_request_code` 导出 curl/requests/httpx/fetch/axios，凭据走环境变量占位。`collect_api_pages` 沿 page_number/offset/cursor 遍历，起点取样本自身取值、只改分页参数，闭合判据只认正面证据，能识破服务端忽略分页参数的情况。分页字段可在 query 或 POST 请求体，游标可来自响应正文、自定义响应头或 `Link: rel=next`。

`replay_network_request` 复用当前浏览器会话与网络栈，由固定 `fetch` 模板在页面上下文发起，浏览器禁止脚本设置的 Header 经一次性 Fetch 拦截改写；目标必须落在授权 origin 内，按非幂等动作处理。**本库不创建独立于浏览器的 HTTP 请求**，也不代理受管浏览器以外的流量。

`manage_network_route` 最多安装 8 条任务级 Fetch 规则，只匹配授权 origin，支持 block、modify_request、mock_response 和 modify_response。Authorization、Cookie、Host、令牌、API Key 等敏感 Header 必须声明为 `Header -> input_key`，执行层最后一刻注入，模型与规则摘要只看到 Header 名。Chromium 拒绝直接覆盖 Host，因此 Host 编译为保留路径与查询的请求 URL authority 重写；`Content-Length` 始终由浏览器计算。每个暂停请求必须恰好走向 continue、fail 或 fulfill，处理异常时优先放行，不能让页面因路由器异常永久等待。

抓取策略默认纯咨询：`check_crawl_policy` 读目标站点 robots.txt 并返回判定、Crawl-delay 与 Sitemap，但不拦截——robots.txt 约束自动化抓取，而本库同样用于登录自家系统这类交互场景，默认拦截会挡掉正当用途。装配时打开 `respect_robots` 后 `navigate`/`open_tab` 才按判定硬拦并按主机限速，生效间隔取站点声明与调用方配置的较大者；4xx 放行，5xx 与取不到判"状态未知"，遵守模式下未知即拒绝（失败关闭）。

## 8. 两条接入通道

**Python API** 是主通道：`launch_browser_toolkit()` 一步装配配置、profile 隔离、网络捕获、结构化采集器与采集程序库，退出时自动关浏览器；`build_browser_toolkit()` 供自管生命周期使用。`toolkit/serialization.py` 是观察与工具结果通往模型的唯一出口——`Observation` 与 `ToolExecutionResult` 是带 `datetime` 和枚举的 dataclass，`json.dumps(asdict(...))` 会直接抛错，且一次观察最多登记 400 个可寻址候选、原样入上下文足以吃满一次请求，因此序列化同时承担 token 预算：候选默认截断到 24 个，真实总数与截断事实一并返回。截断前的次序由 `browser/ranking.py` 统一定义并在驱动截断、模型视图与标注截图三处复用：可输入控件 > 其它控件 > 链接，同组内视口内优先，再按置信度，最后保持文档顺序——按"置信度、角色字母序"排会让两百个导航链接把搜索框挤出视野。"给模型看什么、截断到多少"是消费侧策略，领域层不感知提示词与 token 预算。

**MCP stdio 服务端**服务于不能执行代码或非 Python 的框架。协议 `2025-06-18`，用标准库实现换行分隔 JSON-RPC，不引入新依赖。`core`/`all` 两个工具档位加分类与追加过滤，避免全部 schema 撑爆客户端上下文；另有 MCP 特有的 `open_browser`/`observe`/`close_browser` 三个会话工具——Python 侧用 `async with`，MCP 客户端只能靠两次调用，而 `observe` 在库内是门面方法不是注册工具，不显式暴露客户端就拿不到 `target_id`。

错误语义分两层：协议问题（method 不存在、JSON 不合法、缺 name）回 JSON-RPC error，**工具执行失败回 `isError: true` 的正常响应**——把执行失败也当协议错误会打断连接，而"读到原因后改对参数"正是这类工具最常见的恢复路径。日志固定走 stderr：stdout 是协议通道，一条日志就能破坏分帧。服务端是单会话的，需要并行多页面的客户端应起多个进程。未实现 resources/prompts/sampling 与 Streamable HTTP。

## 9. 故障、证据与可观测性

运行时对超时、取消、连接中断、目标销毁、导航竞争、定位歧义、动作拒绝、业务校验失败、授权拒绝和内部异常进行分类（`ToolFailureKind`）。每项失败记录任务/步骤/页面/动作/耗时/关联证据的中文结构化日志；敏感字段在日志、证据和记忆写入前统一脱敏。

`inspect_page_diagnostics` 只订阅 Runtime 控制台/异常与 Log 事件，按需读取固定页面状态脚本，并与网络记录器的 HTTP 错误摘要组合；动作回执或后置校验失败时执行器立即采样，解决非幂等动作停止重放后没有下一轮诊断机会的问题。诊断不读取网络正文、请求头或 Cookie。

证据文件（截图、快照、JSON/CSV、HAR、落盘正文）一律以 `0600` 创建。**产物加密与自动过期未实现**，长会话需调用方自行清理。

## 10. 测试策略与验收

| 层级 | 重点 | 证据 |
| --- | --- | --- |
| 单元测试 | URL 规范化/脱敏/隔离、候选排序、参数校验边界、完整性门判据、robots 匹配、失败分类 | 纯 Python 确定性测试 |
| 协议契约测试 | CDP 命令、错误、取消、事件路由、扁平 session 附着/分离、先订阅后触发 | 伪造 WebSocket/CDP 服务 |
| 工具契约测试 | 注册表、门面方法、SKILL 文档三者一致；工具数量、速查表覆盖、示例签名绑定、references 双向指引 | `tests/test_toolkit_skill_contract.py` |
| 集成测试 | 受管 Chrome 的启动、定位、导航、输入、下载、iframe、表单、拖放、流量、分页、限速 | 本地确定性演示站点与独立 profile，`WITTY_BROWSER_AUTO_RUN_BROWSER_TESTS=1` |
| 安全回归 | 禁止依赖扫描、敏感数据不落盘、视觉动作默认拒绝、只读门控、脱敏断言 | 依赖锁文件、负向测试和日志断言 |

浏览器内的真实行为（页面脚本、CDP 事件时序）必须由真实 Chrome 集成测试验证，判据取真实效果（如"服务端实际收到过哪些路径"），不做盲实现；假驱动只用于执行层逻辑。测试中不得以"命令已发送"代替业务成功。

## 11. 实施约束

- Python 3.11+，异步内核，完整类型标注；运行依赖只有 `aiohttp`，新增运行依赖需要明确决策并记录理由。
- Python 标识符使用通用英文命名，代码注释、日志和错误信息使用中文。
- 单个源文件不超过 1500 行；按职责拆分，不为未使用能力建立抽象。
- 不维护同步/异步两套内核，不自研 HTTP 客户端、HTML 解析器或全量 CDP 包装。
- 所有"已实现"状态必须有测试或可复现演示证据，并同步更新 `docs/PROJECT_STATUS.md` 与 `docs/change_maintenance/CHANGELOG.md`。
- 第三方项目仅作为问题分解和公开协议参考，来源、许可证、采用/拒绝项见 `docs/research/THIRD_PARTY_REFERENCES.md`。

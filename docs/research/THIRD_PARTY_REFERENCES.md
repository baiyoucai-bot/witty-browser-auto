# 第三方参考与清洁室记录

## 1. 目的与适用范围

本记录用于追溯Witty 浏览器工具库 使用的公开资料、许可证及设计取舍。它不授权引入任何浏览器自动化框架，也不表示已下载、复制或修改其源码。当前运行时代码已经按本项目领域协议和官方 CDP 独立实现；本记录只说明问题分解与资料来源，不构成第三方源码继承关系。

## 2. 来源台账

| 来源 | 官方 URL | 许可证 | 本项目采用的理念 | 明确拒绝/不采用 |
| --- | --- | --- | --- | --- |
| Chrome DevTools Protocol 协议文档 | https://chromedevtools.github.io/devtools-protocol/ | 协议定义仓库 `chrome-devtools/devtools-protocol`：BSD-3-Clause | 直接以官方域、命令、事件和 JSON 消息格式实现所需 CDP 能力；使用 `Target` 扁平 session、`DOM.performSearch` 解析 CSS/XPath，以及 `Fetch.requestPaused` 配合 continue/fail/fulfill 实现任务级路由 | 不生成全量 CDP 包装；不依赖第三方浏览器自动化框架代替协议实现 |
| CDP 协议定义仓库 | https://github.com/ChromeDevTools/devtools-protocol | BSD-3-Clause | 以 `browser_protocol.pdl`、`js_protocol.pdl` 作为版本兼容性和命令字段的权威来源 | 不复制其生成器或将协议仓库作为运行时依赖 |
| Chrome 远程调试文档 | https://developer.chrome.com/docs/devtools/remote-debugging/ | 文档内容遵循 Chrome Developers 网站条款；代码示例通常标注 Apache-2.0，具体页面以页脚为准 | 使用受控 `--remote-debugging-port`、本地发现端点和独立自动化 profile 的运维方式 | 不暴露公网调试端口；不自动接管用户日常 profile 或扫描外部浏览器 |
| Chrome for Testing | https://developer.chrome.com/blog/chrome-for-testing/ | Chrome/Chromium 的分发与许可按官方发布条款及 Chromium 许可证 | 在 CI 或可复现集成测试中选择可固定版本的浏览器二进制 | 不把浏览器二进制或其许可证误当作本项目代码许可证 |
| aiohttp 文档 | https://docs.aiohttp.org/ | Apache-2.0 | 作为异步 HTTP 与 WebSocket 传输候选，承载 CDP 的连接、收发、超时和取消 | 不采用其不存在的浏览器自动化语义；不自研通用 HTTP/WebSocket 客户端 |
| aiohttp 源码与许可证 | https://github.com/aio-libs/aiohttp | Apache-2.0 | 评估连接管理、超时和 WebSocket API 的稳定使用方式 | 不复制源码或内部实现 |
| DrissionPage 文档 | https://www.drissionpage.cn/ | 以项目仓库当前自定义许可证为准，仅允许个人学习和合法非盈利用途 | 参考“浏览器复用、等待与重试、标签页、iframe、下载和网络协同”的问题分解 | 不采用其源码、模块布局、注释、定位 DSL、公开 API 语义或运行依赖 |
| DrissionPage 源码与许可证 | https://github.com/g1879/DrissionPage/blob/master/LICENSE | 自定义非商业许可证；商业使用需版权方授权 | 仅作为独立设计时的能力清单参考 | 不下载后复制、换名改写或派生实现；不作为直接或传递依赖 |
| Chrome DevTools MCP | https://github.com/ChromeDevTools/chrome-devtools-mcp | Apache-2.0 | 参考其把网络请求、控制台消息、脚本求值、截图和性能诊断组织成模型可主动调用的只读工具面 | 不引入其 Node.js/MCP 运行时依赖，不复制工具实现；本项目继续直接使用已有异步 CDP Session |
| Browser Use | https://github.com/browser-use/browser-use | MIT | 参考可注册自定义工具、把浏览器状态和工具结果回灌模型，以及 `max_actions_per_step`、`max_failures` 等有界失败预算 | 不采用其浏览器运行时、Agent API、模型适配层或 Playwright 依赖 |
| Stagehand | https://github.com/browserbase/stagehand | MIT | 参考 `observe/act/extract` 职责划分、动作缓存和页面变化后的自愈思路 | 不采用其 TypeScript SDK、缓存格式、定位实现或云服务依赖 |
| Skyvern | https://github.com/Skyvern-AI/skyvern | AGPL-3.0 | 参考语义定位不足时的视觉观察回退和任务分解理念 | 不引入 AGPL 源码、部署组件、浏览器运行时或工作流定义 |
| mitmproxy | https://github.com/mitmproxy/mitmproxy | MIT | 仅把事件 hook、可编程 addon 和 HAR/流量诊断视为未来可选深度抓包侧车 | 不默认安装根证书、不透明代理浏览器流量、不把完整请求/响应、Cookie 或认证头送入模型 |
| Hermes Agent | https://github.com/NousResearch/hermes-agent | MIT | 参考持续 session、运行中 interrupt/redirect、实时工具输出、activity heartbeat、重启续跑、上下文压缩和最终交付确认的产品控制面 | 不引入其 CLI、Python/Node 工具集、远程执行能力或源码；本项目继续复用自主 CDP、既有 AgentEngine 和领域协议 |
| PenguinHarness | https://github.com/Prism-Shadow/penguin-harness | Apache-2.0 | 参考最小工具集、round 与 session 分离、Trace 可观测、每轮快照和自我评估的持续会话控制面；本轮进一步把工具面按执行阶段收敛，并保留执行前二次校验 | 不引入其 TypeScript harness、Skill 运行时、桌面打包或源码；不复制其工具注册和模型适配层 |
| OpenAI Agents SDK | https://openai.github.io/openai-agents-python/ | MIT（源码仓库） | 参考函数工具 schema、运行时 agent loop、Tracing，以及工具输入 guardrail 在执行前允许/拒绝并把纠正消息回给模型的边界 | 不引入 SDK 运行依赖，不替换本项目 AgentEngine、模型网关、领域协议或本地执行器 |
| Model Context Protocol 规范 | https://modelcontextprotocol.io/specification/2025-06-18/server/index | 规范与官方 SDK 仓库按其 Apache-2.0 许可证 | 依据官方 server/tools 与 stdio 生命周期实现项目级 `initialize`、`tools/list`、`tools/call`，把发现工具适配为现有 function tools | 首版不引入 MCP SDK 依赖，不实现 Streamable HTTP、resources/prompts、sampling 或远程认证，不让 MCP 建立第二套 Agent Loop |
| LangGraph | https://langchain-ai.github.io/langgraph/ | MIT（源码仓库） | 参考条件边根据显式状态路由到工具节点或 `END`，以及 thread/checkpoint 持久化、interrupt 后同任务恢复的有限状态图思想，用于阶段工具选择、任务快照和完成收敛 | 不引入图运行时、LangChain 模型层或其节点/状态类型，不把现有循环改造成第二套编排框架 |
| GraphEngineering | https://github.com/reacher-z/GraphEngineering | 本地参考快照未携带可核验许可证，未引入代码 | 参考 typed event envelope、节点状态/失败码、确定性 ready queue、重试边界、checkpoint 后再调度依赖节点 | 不引入其调度器、事件存储、图运行时或未知许可证源码；仅按本项目 Python/CDP 契约独立实现 |
| Electron | https://github.com/electron/electron | MIT | 使用 main/renderer 进程边界、`BrowserWindow`、托盘、隔离 preload 和本地 Python 子进程守护构建桌面工作台 | 不让 renderer 直接访问 Node.js，不在 Electron WebView 内运行目标站点，不让 Electron 接管独立 Chrome 的 CDP 所有权 |
| QwenPaw | https://github.com/agentscope-ai/QwenPaw | Apache-2.0 | 参考其会话导航、中央对话区、工作区工具入口和桌面客户端的信息架构，仅采用产品布局理念 | 不复制其 React/TypeScript/Less 源码、组件结构、图标、品牌资产或桌面打包实现 |
| OpenAI Codex App | https://openai.com/index/introducing-the-codex-app/ | 产品与文档按 OpenAI 网站条款 | 参考线程式任务切换、克制的桌面工作区、长期任务监督和运行中继续对话的产品理念 | 不复制产品源码、专有素材、品牌标识或像素级视觉；本项目保持独立品牌与浏览器任务语义 |

初始来源访问日期：`2026-07-31`；诊断工具与 Hermes 调研补充日期：`2026-08-05`；PenguinHarness、GraphEngineering、Electron、QwenPaw 与 Codex App 参考补充日期：`2026-08-07`；OpenAI Agents SDK、LangGraph、Browser Use 失败预算与 PenguinHarness 最小工具集复核日期：`2026-08-08`；MCP server/tools、stdio 规范以及 CDP DOM/Fetch 协议复核日期：`2026-08-09`；Hermes 会话控制面与 LangGraph 持久化文档复核日期：`2026-08-10`。PenguinHarness 与 QwenPaw 的 Apache-2.0、Electron 的 MIT 由其官方仓库页面核验；GraphEngineering 快照未能核验许可证，因此没有引入其代码。许可证结论仍应在首次引入具体第三方包、代码片段或浏览器分发物前重新核验其上游 `LICENSE`、版本及分发条款。

## 3. 采用与拒绝的边界

### 3.1 采用的公开思想

- CDP 以官方协议为唯一浏览器控制语义来源，按需实现 `Browser`、`Target`、`Page`、`Runtime`、`DOM`、`Accessibility`、`Network`、`Fetch`、`Input` 等域。
- 使用单一 WebSocket 加扁平 `Target` session 管理浏览器 Target，事件先订阅再触发动作。
- 参考成熟工具对浏览器复用、等待、标签页、下载、iframe 和网络协同的能力分解，但领域接口由本项目独立定义。
- 使用成熟异步 HTTP/WebSocket 库承载传输，避免自研通用网络基础设施。
- 参考 Chrome DevTools MCP 的工具面，把页面就绪状态、控制台、运行时异常和失败网络请求统一为模型可主动调用的只读诊断；动作失败时由执行层立即保存同一份有界摘要，避免非幂等动作停止后丢失现场。
- 外部 MCP 或代理抓包只作为未来侧车：先使用浏览器已有 CDP 事件，因为它无需额外代理、证书或第二套浏览器所有权；只有 CDP 无法覆盖 WebSocket/SSE、代理链和传输层问题时才评估显式启用。
- 参考 Hermes 把持久会话和执行引擎分离：用户可在运行中追加或纠正目标，执行器在安全边界处吸收新指令；工具输出、等待、activity heartbeat 和恢复对用户持续可见，最终答复必须确认实际交付物而不是只复述动作。
- 参考 LangGraph 的 thread/checkpoint 语义，把会话容器与具体任务执行身份分开；恢复、聊天、监督和结果投影都绑定当前 `task_id`，旧任务终态不得覆盖新任务状态。当前实现仍使用既有 SQLite 检查点和 AgentEngine，不增加第二套图运行时。
- 参考 PenguinHarness 的最小工具集与 Trace 思路，每轮只公开当前阶段可推进的工具；参考 LangGraph 条件边把状态到下一阶段的转换显式化；参考 OpenAI Agents SDK 工具 guardrail，在工具真正执行前再次校验并把拒绝原因回灌模型。
- 依据 MCP 官方规范把服务端工具发现与模型工具调用解耦；外部工具经过项目配置、命名空间、数量上限、阶段过滤和执行前白名单后，才进入既有 AgentEngine。

### 3.2 明确拒绝的内容

- Playwright、Selenium、DrissionPage 均不得出现在浏览器运行时、可选运行时或传递依赖中。
- 不复制第三方源码、测试、注释、模块路径、类层次、定位 DSL、错误文案或示例流程；不把 API 改名后作为自主实现。
- 不照搬 DrissionPage 的定位语法，不建设 Selenium 兼容层，不维护全量 CDP 包装。
- 不实现验证码绕过、MFA/认证绕过、访问控制规避、指纹伪装或隐蔽持久化。
- 不把控制台对象、完整堆栈、请求/响应正文、请求头、Cookie 或认证材料直接放进模型诊断；只保留有界文本、脱敏 URL、状态计数和故障分类。

## 4. 清洁室实现规则

1. 实现前先以本项目需求、`domain` 协议和官方 CDP 文档写出功能契约、输入、输出、失败模式和验收测试。
2. 编写代码时只查阅官方 CDP/Chrome 文档及本项目已批准的设计记录；不同时打开第三方实现进行逐行对照或仿写。
3. 若第三方资料启发了能力需求，只在本记录中以“问题/理念”级别记载，不记录或复刻具体算法、类图、命名、源码片段、注释或测试数据。
4. 新实现必须具有独立的模块边界、类型名称、调用流程和测试用例；任何贡献者发现相似实现应停止合入，重新按本项目契约设计并记录原因。
5. 引入依赖前执行依赖清单检查，确认其中不含 Playwright、Selenium、DrissionPage；对每个新增第三方包记录版本、用途、许可证和替代方案。
6. 代码审查应检查：来源可追溯、许可证兼容、无复制痕迹、无框架传递依赖、测试基于公开协议与本地演示站点，而非第三方项目测试。

## 5. 后续维护

新增或更新参考资料时，应在本文件补充 URL、访问日期、许可证、采用理念和拒绝项；若实际引入第三方依赖或分发物，还必须更新需求基线、项目状态和变更维护记录。任何许可证不明、条款不兼容或清洁室边界不能确认的资料，不得作为实现依据。

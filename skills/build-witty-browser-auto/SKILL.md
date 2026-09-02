---
name: build-witty-browser-auto
description: 开发、修改、评审或验证Witty 浏览器工具库的项目专属协议。本仓库是供外部大模型智能体调用的确定性浏览器工具库（Python API 与 MCP stdio 两条接入方式），仓库内不存在任何发起模型请求的代码。用于涉及自研 CDP 内核、浏览器生命周期、页面观察与定位、页面操作工具族、结构化采集与完整性门、采集程序晋升与重放、网络抓包与接口逆向、MCP 服务端、工具契约单一事实源、SKILL 文档契约、脱敏与数据安全边界，或本仓库需求和进度维护的任务。
---

# 构建Witty 浏览器工具库

## 项目定位

本仓库是给**外部大模型智能体**调用的确定性浏览器工具库：由外部智能体（Codex、Claude、Cursor 等）决定下一步做什么，本库保证每一步做得对——参数校验、业务后置条件、脱敏、非幂等防重放、采集完整性门全部由固定代码执行。两条接入方式：能执行 Python 的智能体走 `witty_browser_auto.toolkit.launch_browser_toolkit` 并读 `skills/use-browser-toolkit/SKILL.md`；不能执行代码或非 Python 的框架走 `witty-browser-auto mcp` 的 MCP stdio 服务端。

2026-08-28 架构收敛时删除了全部模型调用侧代码（引擎循环、聊天工作台、Electron/Pi、自我进化），备份在仓库外 仓库外的独立备份目录 `2026-08-28-model-removal/`。**不得在本仓库内复活任何发起模型请求的代码**；"给模型看什么"属于消费侧序列化（`toolkit/serialization.py`），不属于领域层。

## 开始工作

1. 从仓库根目录读取：
   - `docs/requirements/WITTY_BROWSER_AUTO_REQUIREMENTS.md`
   - `docs/PROJECT_STATUS.md`
   - `docs/change_maintenance/CHANGELOG.md`
2. 若存在 `.codegraph/`，先用 CodeGraph 理解或定位代码；不存在时再使用 `rg`。
3. 读取 [项目约束](references/project-constraints.md)。涉及架构、定位、采集、网络或数据安全时，再读取 [架构护栏](references/architecture-guardrails.md)。
4. 选择 `docs/PROJECT_STATUS.md` 中最高优先级且没有依赖阻塞的最小工作单元。
5. 开始编辑前，明确本轮范围、验收命令、风险和不做事项。

## 执行开发循环

重复以下步骤，直到本轮工作通过验证或遇到不可自行消除的阻塞：

1. 把当前工作项标为"部分实现"，写清任务目标、成功条件和验收方式。
2. 先复现问题并保存最小证据，再增加能锁定行为的测试或契约测试。
3. 工具契约只有一个事实源：`toolkit/catalog.py`。新增或修改工具必须在这里声明名称、分类、参数、返回契约和是否可外部调用；registry 据此派生 schema 与执行前校验，门面（`toolkit/facade.py`）提供同名方法。禁止在别处维护第二份工具清单（MCP `core` 档位是唯一例外，且有测试把它钉在注册表上）。
4. 每个工具遵守统一执行契约：
   - **失败是返回值不是异常**：只有参数非法在本地抛 `ToolArgumentError`，业务失败通过 `ToolExecutionResult.success=False` 加可行动的中文原因返回。
   - **副作用动作声明业务后置条件**，条件在动作前必须为假；动作派发成功不等于业务成功。
   - **两路视图分离**：`data` 给调用方完整结果，`model_data` 给有界脱敏视图；原始业务行、完整正文、本机路径不进模型视图。新工具若含批量数据或敏感内容，必须声明 `model_data`。
   - **敏感值经 `inputs` 键名引用**，执行层最后一刻解析；结果、事件与轨迹只保留键名。
   - **非幂等动作失败或结果未知时不自动重试**。
5. 浏览器内的真实行为（页面脚本、CDP 事件时序、真实站点语义）必须用真实 Chrome 集成测试验证（`WITTY_BROWSER_AUTO_RUN_BROWSER_TESTS=1`），判据取真实效果（如"服务端实际收到过哪些路径"），不做盲实现；假驱动只用于执行层逻辑。
6. 文档即接口：`skills/use-browser-toolkit/SKILL.md` 是外部智能体唯一的入口说明，契约测试（`tests/test_toolkit_skill_contract.py`）绑定工具数量、速查表覆盖、示例签名与 references 双向指引。新增工具必须同步主文档速查表、数量描述与门面方法，否则契约测试会失败。
7. 使用中文记录问题、理由、验证和风险；不得记录密钥、Cookie、令牌或完整敏感响应。
8. 运行聚焦测试，再运行 `uv run pytest -q` 全量测试、`ruff check` / `ruff format --check`、`python -m compileall src`。
9. 只有存在可复现证据时，才把工作项标为"已实现"；否则保持"部分实现"或"未实现"。
10. 更新 `docs/PROJECT_STATUS.md`（能力表、已知边界、下一阶段入口）和 `docs/change_maintenance/CHANGELOG.md`（五字段格式）。
11. 运行 `python skills/build-witty-browser-auto/scripts/check_project_state.py .` 检查维护状态。

## 保持核心边界

- 使用 Python 异步内核；运行依赖只有 `aiohttp`，新增运行依赖需要明确决策并记录理由。
- 领域层依赖本项目定义的协议（`domain/protocols.py`），不直接依赖 CDP 传输或具体数据库实现。
- 浏览器运行时基于自研异步 CDP 内核（`cdp/`）；禁止引入 Playwright、Selenium、DrissionPage 等自动化运行时，也不能成为传递依赖。
- 把动作发出、动作回执和业务结果校验拆开；每个有副作用的动作必须有后置条件。同一页面的写动作保持串行，只并行无副作用的观察。
- 本库不发起任何模型调用、不做整站爬取编排、不管理调用方的会话生命周期；这些是外部调用方的职责，文档里如实写明而不是悄悄承接。
- 终态与等待语义（`finish`/`ask_user`/`block`/`wait_until`）只保留契约定义供兼容校验，不得开放给外部调用或暴露成门面方法。
- MCP 服务端的 stdout 只承载协议消息，日志固定走 stderr；协议问题回 JSON-RPC error，工具失败回 `isError` 正常响应，不中断连接。
- 模型只能提交经过字段白名单校验的受控规格（如 CSS 提取规格）；固定代码持有页面执行模板，禁止把调用方提供的 JavaScript 或 XPath 拼进页面执行代码。

## 完成条件

- 当前工作项的验收测试、全量测试、格式和静态检查全部通过（含 SKILL 契约测试）。
- 无未解释的跳过测试、宽泛异常捕获、静默失败或敏感信息日志。
- 新增行为有中文错误信息和必要的中文注释。
- `docs/PROJECT_STATUS.md` 与真实实现一致，并列出仍未实现的内容；工具数量等口径与代码实测一致。
- 全局变更记录包含时间、问题、文件、理由、验证和风险。
- 重大架构或安全变更经过独立审查，审查问题已修复或明确记录。

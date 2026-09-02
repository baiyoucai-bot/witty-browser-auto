# 已验证程序重放设计

状态：调研后落地设计（未实现）  
日期：2026-08-27  
范围：在现有 Loop Engine、`VerifiedPlan`、`CollectionExtractionSpec` 上，补齐「首跑观察编译 → 存储前验证门 → 确定性重放 → 失配回退重编译」闭环。

## 1. 问题与目标

当前产品已经具备三段能力，但还没有合成一条可证明可靠的程序生命周期：

| 已有能力 | 位置 | 缺口 |
| --- | --- | --- |
| 模型观察后提交受控规格，固定代码批量采集 | P1-010 / `CollectionExtractionSpec` | 规格成功一次后，缺少「独立复跑验证再晋升」 |
| 任务成功后异步写 `VerifiedPlan`，命中后逐步执行并校验 | `memory.models.VerifiedPlan` / `_try_fast_path` | 步骤仍是线性宏，不是带状态谓词的程序；成功任务即写入，没有存储前验证门 |
| 前置/后置失配立即回退观察循环 | `_fast_path_fallback` | 回退后重新成功的路径，不会替换旧程序；语料只追加、不精炼 |

目标是把「边看边写、遇错纠正」收敛成可审计的产品语义，而不是再引入一个每轮都烧 Token 的浏览器智能体：

1. **冷启动**：模型只负责发现与提交受控规格/路径候选。
2. **编译**：把一次成功轨迹或一份已闭合的采集规格编译成可重放程序。
3. **存储前验证门**：在干净状态下重跑程序，由独立完成条件确认后才入库。
4. **热重放**：后续同场景优先跑程序，逐步检查页面谓词，命中则零模型或极少模型。
5. **失配纠正**：谓词失败时回退观察循环；新成功后再编译，经验证门后按去重签名替换旧程序。

非目标（与现有约束一致）：

- 执行模型不得修改 `src/witty_browser_auto` 源码；框架缺陷仍走后台问题库与维护流程。
- 不引入 Playwright / Selenium / DrissionPage。
- 不把网页正文、原始业务行、敏感 Header、凭据写入程序或模型修复上下文。
- 不把视觉拖拽、安全挑战写入可重放程序。
- 不把「记录一次真人操作」直接当成已验证程序；示范事件最多是候选。

## 2. 市场对照与本项目映射

市面相关方案可分成四层；本设计只吸收其中与 CDP 原生 Loop Engine 兼容的一层。

| 层级 | 代表 | 做法 | 对本项目 |
| --- | --- | --- | --- |
| A. 开发期边看边改 | Chrome DevTools MCP、Playwright Planner/Generator/Healer、`lackeyjb/playwright-skill` | 编码智能体看控制台/网络/页面后改仓库代码 | 只用于维护者开发环境；不进入执行模型 |
| B. 动作缓存 / 自愈 | Stagehand `act` cache + selfHeal、Heym `saveStepsForFuture`/`autoHealMode` | 缓存选择器，失败再问模型 | 可参考缓存键与失败换路，但不依赖其 SDK |
| C. 录制/首跑编译脚本 | Browser Use Scripts / Workflow-Use、Skyvern Code Caching、Kadoa Code Generation | 首跑生成脚本，后续复用，坏了再修 | 语义最接近；本项目用领域程序，不用外部脚本运行时 |
| D. 验证后入库程序语料 | PreAct（arXiv:2606.17929）、Muscle-Mem、AutoScraper | 编译为可执行物；重放前检查；入库前独立验证 | **主参考**：状态机 + 逐步谓词 + Verify-before-Store |

本项目取 D 的结构，复用 C 的产品叙事，把 A 留在开发侧车，把 B 的「缓存命中/失配」落在现有 `best_plan` / `fast_path_fallback` 上。

## 3. 核心抽象：VerifiedProgram

在 `VerifiedPlan` 之上引入程序语义，而不是平行再建一套存储。首版建议：

- 保留表名 `verified_plans`，通过 `metadata.program_version = 2` 与 `kind` 区分旧线性计划。
- 或新增 `verified_programs`，迁移时把旧 `VerifiedPlan` 视为单路径程序。

推荐程序模型：

```text
VerifiedProgram
  program_id
  kind: interaction | collection
  scope / scenario_key / site_origin / path_template
  dedup_signature          # 场景 + 入口 URL 模板 + 结构指纹 + 参数模式
  structure_fingerprint    # 页面结构版本，不等于单次 observation.fingerprint
  parameters               # 从具体值提升出的输入键（如 order_id）
  states[]                 # 状态机节点
  transitions[]            # 边上动作
  completion               # 业务完成谓词 / 完整性门引用
  confidence / evidence_id / stats
```

### 3.1 状态与转移

每个 `ProgramState` 必须可在无模型情况下判定真假：

| 谓词类型 | 来源 | 用途 |
| --- | --- | --- |
| `url_match` | 规范化 URL / path template | 确认仍在授权入口 |
| `role_name_exists` | 现有候选 `role`+`name` | 兼容当前 `PlanStep` 定位 |
| `css_count_range` | 确定性 DOM 计数 | 列表页「至少 N 行」 |
| `text_absent` / `text_present` | `ExpectedCondition` | 业务后置 |
| `collection_progress` | 采集完整性摘要 | 页码/总数闭合中的中间态 |
| `structure_fingerprint_match` | 结构指纹 | 大改版时整表失效 |

每个 `ProgramTransition` 只能引用现有确定性动作面：

- 交互类：`NAVIGATE` / `CLICK` / `INPUT_TEXT` / `SELECT` / `SCROLL` / `WAIT`（不含 `DRAG`/`VISUAL_DRAG`）
- 采集类：绑定一份已校验的 `CollectionExtractionSpec`，由固定采集器执行，而不是逐步点选每一行

旧 `PlanStep` 可机械升级为「单入口状态 → 动作 → 后置状态」的退化状态机，保证兼容。

### 3.2 两种程序形态

**interaction 程序**  
对应表单填写、登录后跳转、筛选后进入列表等短交互。重放器逐步：检查状态谓词 → 执行转移 → 检查后置谓词 → 写检查点。

**collection 程序**  
对应 P1-010。程序本体不是「点 N 次下一页」的脆弱宏，而是：

1. 入口状态谓词（列表容器、表头、分页控件结构指纹）
2. 一份 `CollectionExtractionSpec`
3. 完成谓词引用现有完整性门（声明总数闭合 / 总页覆盖 / 稳定滚动终点 + 双产物）

也就是说：**采集的确定性执行器已经存在；缺的是规格晋升与失效重编译。**

### 3.3 结构指纹，而不是观察指纹

`observation.fingerprint` 适合「当前这一屏是否还是同一次观察」。程序需要更稳的 `structure_fingerprint`：

- 输入：主框架可见区域的角色/标签骨架、关键容器选择器哈希、分页模式、字段名集合；不含单元格业务值。
- 变化策略：
  - 相同：允许命中重放
  - 轻微漂移（例如多一个无关 banner）：仍可尝试，但降低置信度；连续失败则失效
  - 结构破坏（行选择器计数归零、分页模式消失）：直接 miss，不执行任何副作用动作

## 4. 生命周期

```text
观察循环成功
    │
    ▼
CompileCandidate          # 从轨迹或规格生成候选程序（可含参数提升）
    │
    ▼
VerifyBeforeStoreGate     # 干净状态重跑 + 独立完成条件
    │ 通过
    ▼
UpsertByDedupSignature    # 同签名替换旧程序；不同签名追加
    │
    ▼
ReplayOnLaterTasks        # 逐步谓词检查；命中零模型
    │ 失配
    ▼
FastPathFallback          # 现有回退
    │
    ▼
Observe/ModelRepairSpec   # 只修规格或路径候选，不修框架源码
    │ 再次成功
    └──► CompileCandidate ...
```

### 4.1 编译（Compile）

编译器是确定性代码，不是执行模型随便写文件。

交互轨迹编译输入：

- 本任务已执行且后置条件通过的动作序列
- 每步前后的 URL、结构指纹、候选定位配方
- 任务 `inputs` 键集合（用于参数提升；值本身不入库）

采集规格编译输入：

- 一次已闭合的 `CollectionExtractionResult.model_summary()`（无原始行）
- 当时的 `CollectionExtractionSpec`
- 入口页结构指纹与分页模式证据

编译输出必须拒绝：

- 含静态敏感值的步骤
- 视觉/挑战动作
- 无法表达为现有 `ExpectedCondition` / DOM 计数谓词的步骤
- 未闭合的部分采集结果

### 4.2 存储前验证门（Verify-before-Store）

这是相对现有 `save_plan_later` 最关键的增量。PreAct 的实证结论是：没有这道门，错误程序会在热重放中把成功率拉垮。

门的最低标准：

1. **独立于首次成功现场**：在同一授权 origin 下重新进入入口 URL（或从检查点声明的可重启状态开始），不得直接信任内存中的当前 DOM。
2. **跑的是候选程序本身**，不是再让模型做一遍。
3. **完成条件与用户任务完成条件一致**：交互任务看业务后置；采集任务看完整性摘要门，而不是「脚本没抛错」。
4. **有副作用预算**：验证门默认只接受幂等或可安全重跑的程序；含提交/支付/删除的程序首版不得自动晋升，只能保持人工确认或保持观察循环。
5. **失败则丢弃候选**，记录 `program_verify_rejected`，可把原因写入失败教训记忆；不得入库。

实现落点建议：

- 新模块 `runtime/program_gate.py`：纯判定与编排接口
- 由 `BackgroundMemoryRuntime` 在任务成功后异步调度，不阻塞用户可见终态回复；但程序在门通过前对后续任务不可见
- 工作台事件增加 `program_compiled` / `program_verify_passed` / `program_verify_rejected` / `program_promoted` / `program_replaced`

### 4.3 重放（Replay）

扩展现有 `_try_fast_path`，而不是另写执行引擎：

1. `best_program_cached(...)` 按 scope、scenario、URL 模板、结构指纹取候选。
2. 从初始状态开始，对每个状态评估谓词；失败则 `_program_fallback`（复用 `_fast_path_fallback` 事件语义）。
3. 执行转移前再次确认动作目标唯一、可见、启用、中心命中（沿用现有定位校验）。
4. 非幂等动作结果未知或后置失败：保持现有 `BLOCKED` 语义，禁止自动重放副作用。
5. 全程写 `loop_path_events` 与逐步检查点。

采集程序重放：

1. 校验入口谓词与结构指纹。
2. 直接调用现有结构化采集执行器。
3. 用完整性摘要判定成功；失败则回退观察，允许模型只更新规格字段/分页选择器后再次编译。

### 4.4 失配纠正（Repair Spec，不是 Repair Framework）

纠正对象是**程序/规格**，不是项目源码。

| 失败分类 | 动作 |
| --- | --- |
| 入口结构指纹不匹配 | miss，不降权过猛；观察后重编译 |
| 单步定位歧义/消失 | 回退观察；模型用现有工具重选定位配方 |
| 采集字段空置或行数归零 | 规格失效；重新 `inspect → submit spec` |
| 分页控件变化 | 只作废分页段，保留字段规格作候选 |
| 完整门未闭合 | 不得晋升；保持部分结果语义 |
| 驱动/工具内部异常 | 仍走能力缺口 / 后台维护，不在此闭环内改代码 |

执行模型可新增的工具面应极窄，例如：

- `propose_program_patch`：提交对当前程序某个 state/transition/spec 字段的候选补丁
- 补丁只进入编译器与验证门，不直接写生产程序表

禁止重新打开已关闭的执行层源码热修开关。

## 5. 与现有模块的改动点

| 模块 | 改动 |
| --- | --- |
| `memory/models.py` | `PlanStep` 保留；新增 `ProgramState` / `ProgramTransition` / `VerifiedProgram`；旧计划适配器 |
| `memory/store.py` | 查询增加结构指纹与 dedup upsert；未过门的候选表或 `enabled=false + pending_verify` |
| `memory/background.py` | `save_plan_later` 改为 `compile_and_verify_later` |
| `agent/path_command.py` | 从 transition 生成 `ActionCommand`；谓词失败返回 miss |
| `agent/engine_runtime.py` | `_try_fast_path` 升级为程序重放；补充程序事件 |
| `agent/task_completion.py` | 成功后不再直接 `save_plan_later`；改为投递候选 |
| `domain/extraction.py` | spec 增加可选 `structure_fingerprint` / `program_id` 关联字段（或放 metadata） |
| `browser/extraction.py` | 采集结果摘要增加可供门使用的稳定性证据，不含原始行 |
| `runtime/program_gate.py` | 新模块：干净重跑、完成判定、拒绝原因 |
| `runtime/program_compile.py` | 新模块：轨迹/规格 → 候选程序 |
| 工作台前端 | 展示程序晋升/拒绝/替换事件；不展示原始业务行 |

测试最小集：

1. 旧 `VerifiedPlan` 仍可重放。
2. 交互程序：验证门通过才可被 `best_program` 命中。
3. 验证门失败的候选对后续任务不可见。
4. 重放中单步谓词失败回退观察，不执行后续副作用步骤。
5. 采集程序：结构指纹变化后 miss，模型更新 spec 并再次过门后替换旧程序。
6. 含非幂等提交的轨迹拒绝自动晋升。
7. 程序 JSON 中不得出现任务输入值或表格业务值。

## 6. 分阶段交付

### P0 — 规格晋升门（最小可用）

只覆盖 collection 程序：

- 一次闭合采集成功后，把 `CollectionExtractionSpec + structure_fingerprint` 作为候选
- 同入口重新跑采集器作为验证门
- 通过后供后续同场景任务直接走采集器，不再让模型重新选字段

验收：本地演示列表页两次全量采集，第二次无模型选规格；人为改行选择器后第一次失败回退，修好并过门后恢复热路径。

### P1 — 交互状态机

把 `VerifiedPlan` 升级为带状态谓词的 interaction 程序；`_try_fast_path` 改为逐步谓词重放；成功写入改走验证门。

验收：表单任务冷启动一次，热启动零模型；删除目标按钮后回退观察并重编译替换。

### P2 — 局部补丁与示范编译

- 允许对单个 transition/spec 字段提补丁，经门后替换
- 真人操作事件在补齐前后置条件并重复验证后，才可进入编译器

验收：PROJECT_STATUS 中「示范自动生成代码」项从待实现改为部分实现，且仍禁止无后置条件的盲录宏。

## 7. 风险与对策

| 风险 | 对策 |
| --- | --- |
| 验证门本身产生业务副作用 | 首版限制幂等/只读采集/可回滚测试账号；提交类需显式策略 |
| 结构指纹过严导致几乎不命中 | 分层：硬匹配关键骨架，软匹配次要区域；用失败统计调阈值 |
| 结构指纹过松导致错页执行 | 硬约束授权 origin、scenario_key、关键容器计数；非幂等失配即停 |
| 程序语料膨胀 | dedup 替换 + 置信度衰减 + 最大保留数 |
| 与「执行模型不改代码」边界混淆 | 文档与配置持续强调：修的是程序库，不是框架源码 |
| 把商业反爬/挑战自动化塞进程序 | 挑战与未知视觉动作继续排除在程序动作表外 |

## 8. 决策摘要

1. **要做的「边看边写」**：看的是页面观察与诊断，写的是受控程序/规格，不是项目源码。
2. **要做的「遇错纠正」**：纠正程序库；框架缺陷仍走维护流。
3. **对标主线**：PreAct 的 verified compile–replay，而不是纯 Stagehand 缓存或纯 Browser Use 云脚本。
4. **落地杠杆**：先给 P1-010 规格加验证门与结构指纹，再升级 `VerifiedPlan` 为状态机程序。
5. **成功判据**：同场景第二次及以后运行，模型介入次数与耗时明显下降；页面改版后能自动 miss 并在重新成功后替换旧程序，而不是静默写坏数据。

## 9. 参考

- PreAct: Computer-Using Agents that Get Faster on Repeated Tasks — https://arxiv.org/abs/2606.17929
- Browser Use Rerunnable Scripts — https://docs.browser-use.com/cloud/agent/scripts
- Workflow Use — https://github.com/browser-use/workflow-use
- Stagehand caching / self-healing — https://docs.stagehand.dev/
- Muscle-Mem — https://github.com/pig-dot-dev/muscle-mem
- AutoScraper — https://arxiv.org/abs/2404.12753
- Skyvern Code Caching — https://www.skyvern.com/products
- Chrome DevTools MCP（开发期反馈环） — https://developer.chrome.com/blog/chrome-devtools-mcp
- 本仓库：`memory/models.py`、`domain/extraction.py`（撰写时另参考的 `skills/build-witty-browser-auto/references/loop-engine.md` 与 `agent/engine_runtime.py` 已随 2026-08-28 架构收敛移除，备份在仓库外）

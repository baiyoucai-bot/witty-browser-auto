# PenguinHarness 与 LLM Wiki 记忆决策

- 记录时间：`2026-08-08 08:48 CST`
- 结论：当前不增加 `llm-wiki` 运行依赖；先完成非阻塞 URL 执行记忆。PenguinHarness 的评测驱动自我进化只借鉴设计闭环，后续作为独立后台系统实施。

## 当前记忆分层

1. 会话与执行事件：SQLite 保存聊天、任务状态和有界轨迹，供对话监督解释当前运行。
2. URL 操作经验：按项目、租户、账号和站点隔离，记录注意事项、加载条件、定位、恢复、导航、数据提示和失败教训；成功/相关失败会增信或衰减。
3. 已验证快速计划：绑定业务场景、URL 模板和起始页面指纹，只有确定性业务校验成功后保存；命中后可跳过模型执行。
4. 工程维护知识：需求、Skill、架构、变更记录和可回滚工件保存在项目文件中，目前没有独立 Wiki 服务。

## PenguinHarness 对照

来源：

- <https://github.com/Prism-Shadow/penguin-harness>
- <https://penguin.ooo/blog/introducing-penguinharness/>

PenguinHarness 公布的自我进化闭环包括 benchmark、并行 Evaluator、Optimizer、每轮快照、版本 N 到 N+1、Trace 回放和 Agent Tuning Skills。当前项目已有轨迹、结果校验、失败教训、快速路径、受控代码修复、补丁快照与回滚，但此前没有直接引用 PenguinHarness，也没有完整实现“固定评测集 -> 多评测器评分 -> 优化候选 -> 留出集回放 -> 版本晋升”的闭环。

后续借鉴边界：

- 从真实轨迹生成脱敏候选样本，但由固定 benchmark 清单决定是否进入评测集。
- 多评测器只产生评分与证据，Optimizer 只生成候选 Skill、提示词或配置版本。
- 每轮先快照；候选必须在隔离环境回放，并同时通过正确率、完整性、耗时、模型成本和人工介入率门槛。
- 只有留出集通过后才能晋升；失败自动保留旧版本。整个流程由低优先级后台队列运行，不能修改正在执行的任务。

## LLM Wiki 决策

评估来源：

- <https://github.com/nashsu/llm_wiki>
- <https://github.com/nvk/llm-wiki>

`nashsu/llm_wiki` 适合把 PDF、DOCX、Markdown 等来源增量编译为可追溯 Wiki，并提供本机 API/MCP、混合检索、知识图谱、持久摄取队列和异步人工 Review。`nvk/llm-wiki` 提供脱敏会话检查点、反馈候选、显式晋升和紧凑只读查询协议。这些能力适合未来的项目文档、业务规则、Skill 说明和跨项目知识，不适合替代当前按页面指纹和执行证据约束的 URL 操作记忆。

当前不直接接入的原因：

- 当前主要瓶颈是浏览器执行速度和运行纠错，不是大规模文档检索。
- 外部 Wiki 服务会增加进程、配置、索引一致性和查询延迟，并与现有 SQLite 作用域模型形成双写。
- Wiki 内容不能作为当前页面事实、动作成功证据或快速路径晋升依据。

未来出现大量业务文档和跨项目知识问答需求时，以可选 `KnowledgeProvider` 接入：后台摄取、后台查询、内存缓存、只读有界摘要；服务不可用时主任务继续。URL 操作记忆继续由本项目持有，不向 Wiki 双写。

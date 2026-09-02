# 数据采集深指南：DOM 结构化采集与程序重放

目录：结构检查 → 提交采集 → 完整性门语义 → 逐条详情与过滤 → 采集程序重放与晋升纪律 → 网络数据导出。

## 为什么不自己写 CSS 循环抓

`run_structured_extraction` 做的不只是"按选择器取文本"：首页复位、页面指纹等待、跨页去重、过滤、完整性校验、断点续采、0600 私有 JSON/CSV 导出全部由固定代码完成。你自己写循环最容易错的三件事——把加载中间态当采集完成、跨页重复计成新记录、跑到页数上限就宣布取全——它都替你挡了。

## 第一步：只读结构检查

```python
inspection = await toolkit.inspect_collection_structure()
for candidate in inspection.data["candidates"]:
    candidate["candidate_id"]      # 交给下一步的句柄
    candidate["row_selector"]      # 识别出的行选择器
    candidate["row_count"]         # 当前页行数
    candidate["field_hints"]       # 每列的选择器、标签与取值来源
    candidate["pagination_hints"]  # 识别出的分页控件（下一页/页码/加载更多/滚动）
    candidate["detail_hints"]      # 可进入详情页的入口（用户要逐条详情时用）
```

这一步不输出任何业务数据，只输出结构事实。多个候选时选行数和字段数最符合目标列表的那个。

## 第二步：提交采集

```python
result = await toolkit.run_structured_extraction(
    collection_name="订单列表",
    candidate_id=inspection.data["candidates"][0]["candidate_id"],
    max_pages=50,
)
result.data["去重后总数"], result.data["完整"]
result.evidence[0].path            # 私有 JSON；识别到记录数组时同时有 CSV
```

可选参数：

- `unique_field_id`：指定去重键字段；省略时由代码从字段提示里选稳定列。
- `detail_field_id`：用户要求"每条都要详情"时，从候选 `detail_hints` 里选详情入口，代码会逐条进入详情页并合并字段。列表完整不能替代详情覆盖——目标里有"详情"时必须传这个。
- `filters`：`[{"field_id": ..., "operator": "equals|not_equals|contains|starts_with|ends_with", "value": ...}]`，最多 20 条，过滤发生在导出前、去重后。
- `max_pages`（上限 500）/ `max_items`（上限 100000）：预算上限，不是完成判据。

## 完整性门语义（最重要的一节）

`success=True` 只在 `complete` 且有强完成证据时给出。强证据只有三种：页面声明总数与去重计数闭合、声明总页数全部覆盖、稳定分页终点（加载更多控件消失/禁用、无限滚动连续多轮无新增）。

只是"跑到 `max_pages` 上限"或"有失败页"，一律 `success=False`，`message` 会说明缺了什么。此时不要把已导出的部分数据当成全量交付——这正是这道门存在的原因。正确动作：按提示调大 `max_pages`、处理失败页原因，或改走接口采集路径。

安全挑战中断采集时（详情页弹验证码），`message` 会明确说明，页面停在挑战上；先处理挑战再重试。

## 采集程序：第二次就不用再检查结构

`run_structured_extraction` 成功且证据闭合时，这份规格会自动走存储前验证门：重新导航回入口 URL、在干净状态下执行结构探针，通过才按 `作用域+任务目标+页面路径+结构指纹` 存入本机程序库。之所以要"重进入口再验证"，是因为首跑结束时页面停在末页，规格在那个状态下当然匹配；能证明可复用的只有从入口冷启动仍然成立。

下次同场景任务先试重放：

```python
replay = await toolkit.replay_collection_program()
if replay.success:
    records_path = replay.evidence[0].path      # 与 run_structured_extraction 同格式产物
    summary = replay.data                        # 含 replay=True 与 program_id
else:
    reason = replay.message                      # 失配原因，人话
    fallback = replay.data.get("fallback")       # "inspect_collection_structure"
```

重放的纪律与首跑完全相同：

- 执行前先做入口结构探针（行数、字段取值率、唯一键覆盖、分页控件可读性），站点改版会在真正采集前被识破，程序自动降权，你回退到结构检查重新编译即可。
- 重放结果仍要过同一道完整性门，弱证据一律 `success=False`。
- 任务输入值（手机号、账号）不会被固化进跨任务程序——含输入值的规格在晋升时就被拒绝了。
- 连续失败的程序自动禁用，不会反复浪费重放尝试。

`replay_collection_program` 无参数、不依赖观察；任务目标文本决定场景键，同一目标措辞差异过大可能建多份程序，尽量用稳定的目标描述。

## 网络数据导出：数据走接口时的更优解

页面数据来自 JSON 接口时，直接导出接口响应比解析 DOM 稳定得多。捕获范围：任务授权 origin 内浏览器既有 XHR/Fetch 的成功 JSON 响应。

```python
await toolkit.wait_network_response("/api/orders", timeout_seconds=15)
inspection = await toolkit.inspect_network_data()
for candidate in inspection.data["candidates"]:
    candidate["candidate_id"], candidate["endpoint"], candidate["record_path"]

await toolkit.export_network_response(
    collection_name="订单接口数据",
    candidate_id=inspection.data["candidates"][0]["candidate_id"],
)
```

翻页会产生多个同接口响应，把它们一起交给 `candidate_ids`，代码按规范化 JSON 聚合去重：

```python
await toolkit.export_network_response(
    collection_name="订单接口数据",
    candidate_ids=[c["candidate_id"] for c in inspection.data["candidates"]],
)
```

完整性判定与 DOM 采集同源：只有声明总数或总页数闭合才标记完整；单个响应默认不完整——一页数据不是全量。要主动把分页翻完而不是靠页面恰好加载过，用 `collect_api_pages`（见 `references/api-reverse.md`）。

结构摘要（路径、状态、字段结构）在 `data` 里，原始记录只进私有导出文件，不进任何摘要。

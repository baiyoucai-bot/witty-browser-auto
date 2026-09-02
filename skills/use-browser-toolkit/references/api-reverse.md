# 接口逆向深指南：抓包、重放、契约剖析、代码导出与分页取全

目录：流量检查 → 读正文 → HAR 导出 → 请求重放 → WebSocket 帧 → 接口契约剖析 → 代码导出 → 分页取全 → 路由改写。

## 流量检查（抓包视角）

覆盖浏览器发生的**全部**交换：不限资源类型、不限状态码、不限 MIME，返回完整 URL、请求头（含浏览器实际附带的 Cookie）、响应头、时序分解、发起方调用栈。排查"接口为什么 401""真实接口地址是什么""请求头到底带没带 token"用它。

```python
traffic = await toolkit.inspect_network_traffic(url_contains="/api/", limit=50)
for item in traffic.data["exchanges"]:
    item["exchange_id"]           # 后续所有操作的句柄
    item["status"], item["method"], item["url"]
    item["request_headers"]       # 完整 Header 值，含 Cookie
    item["timing"]                # dns / connect / ssl / send / wait / receive
    item["initiator"]             # 发起方类型与调用栈
    item["security_details"]      # TLS 协议、加密套件、证书签发方与有效期（HTTPS 才有）

failed = await toolkit.inspect_network_traffic(only_failed=True)
errors = await toolkit.inspect_network_traffic(
    status_min=400, status_max=599, resource_types=["XHR", "Fetch"],
)
```

## 全文搜索定位来源

逆向的第一步往往是"页面上这个订单号/价格/token 是哪个接口给的"。正文本就已抓在内存里，用 `search_network_traffic` 直接搜，不必逐条 `read_network_body`：

```python
# 默认在请求体 + 响应体里搜
hit = await toolkit.search_network_traffic("SO-8899")
for match in hit.data["matches"]:
    match["exchange_id"]     # 直接接 read_network_body / analyze_api_endpoint
    match["part"]            # response_body / request_body / request_header / ...
    match["match_count"]
    match["snippet"]         # 命中处上下文片段

# 找"哪个请求带了这个 token"：搜 Header
carrier = await toolkit.search_network_traffic("eyJhbGci", scope="headers")
carrier.data["matches"][0]["field_name"]   # Authorization

# 缩小范围
await toolkit.search_network_traffic(
    "下单成功", scope="all", url_contains="/api/", resource_types=["XHR", "Fetch"],
)
```

`scope` 可选 `response_body`（默认之一）、`request_body`、`body`（默认，两个体都搜）、`headers`、`websocket`、`sse`、`all`。默认大小写不敏感，`case_sensitive=True` 精确匹配。每次交换至多命中一条、取最新的 `limit` 条。片段只回给调用方进程，模型侧只拿交换定位与命中次数。

## 读正文

```python
body = await toolkit.read_network_body(exchange_id, part="response")
body.data["text"]                 # 原文；二进制则是 base64
body.data.get("json")             # 正文是 JSON 时自动解析
request_body = await toolkit.read_network_body(exchange_id, part="request")
```

正文按需采集：默认对 XHR/Fetch/Document/Script/Stylesheet 抓取，单体上限 2 MiB、全局预算 64 MiB；图片、音视频、字体默认不抓，需要时在配置 `traffic.body_resource_types` 里加。

**超过 2 MiB 的大响应会落盘而不是丢掉**（大导出接口常见）。这类正文不进内存，`text` 为 `None`，改从 `spill_path` 读文件：

```python
body = await toolkit.read_network_body(exchange_id)
if body.data["available"]:
    text = body.data["text"]
else:
    path = body.data.get("spill_path")      # 0600 私有文件，二进制按原始字节写入
    text = Path(path).read_text() if path else None
    reason = body.data["reason"]             # 没落盘时说明原因
```

落盘上限默认单体 64 MiB、全局 256 MiB（`traffic.spill_body_bytes` / `traffic.max_total_spill_bytes`，前者设 0 关闭落盘）。超出上限或预算用尽时只保留长度与原因，`reason` 会说明是哪一种。路径只回调用方进程，模型侧只知道"正文已落盘"。

## HAR 导出

```python
har = await toolkit.export_network_har("下单流程", url_contains="/api/")
har.data["har_path"], har.data["entry_count"]
har.data["websocket_count"], har.data["sse_count"]
```

HAR 1.2 私有文件（0600），可直接导入 Reqable、Charles 或浏览器开发者工具。标准字段之外还有几处扩展：WebSocket 连接与帧写入 `log._websockets`；SSE 消息挂在自己那条 entry 的 `_serverSentEvents` 上（SSE 是普通 HTTP 请求，不会被当成 WebSocket）；TLS 与证书信息写入 entry 的 `_securityDetails`。`include_bodies=False` 时帧与 SSE 消息只留元数据不留正文。

排查证书类问题（过期、协议降级、签发方不符）直接看 `security_details` 或 HAR 里的 `_securityDetails`：

```python
detail = traffic.data["exchanges"][-1]["security_details"]
detail["protocol"], detail["cipher"], detail["issuer"]
detail["valid_from"], detail["valid_to"]     # Unix 时间戳
detail["san_list"]                            # 最多 20 项，超出时 san_truncated 为真
```

## 请求重放与编辑重发

只给 `exchange_id` 就是原样重放；再给其他字段就在原请求基础上逐项覆盖。请求在页面上下文用 `fetch` 发起，复用当前浏览器会话和 Cookie：

```python
again = await toolkit.replay_network_request(exchange_id=exchange_id)
again.data["status"], again.data["body_text"]

await toolkit.replay_network_request(
    exchange_id=exchange_id,
    method="POST",
    body='{"page": 2, "size": 100}',
    headers={"Content-Type": "application/json"},
)
await toolkit.replay_network_request(
    exchange_id=exchange_id,
    headers={"Cookie": "SESSION=other"},     # 换会话试探
    remove_headers=["If-None-Match"],
)
```

要点：这是非幂等动作，真实打到服务端，失败不自动重试；目标 URL 必须在任务允许 origin 内；`Cookie`/`Host`/`Origin`/`Referer` 这类浏览器禁止脚本设置的 Header 由一次性 Fetch 拦截补齐，可以照常覆盖；`Content-Length`/`Connection`/`Transfer-Encoding` 由浏览器计算，写了会被忽略；重放请求本身也进流量日志，可再次检查。

## WebSocket 帧

帧既不是请求体也不是响应体，`read_network_body` 对 WebSocket 交换必然落空；实时行情、聊天、推送看内容用这个：

```python
traffic = await toolkit.inspect_network_traffic(resource_types=["WebSocket"])
exchange_id = traffic.data["exchanges"][-1]["exchange_id"]

frames = await toolkit.read_websocket_frames(exchange_id)
for frame in frames.data["frames"]:
    frame["direction"], frame["opcode"], frame["payload"]
    frame.get("json")             # 帧正文是 JSON 时自动解析

ticks = await toolkit.read_websocket_frames(
    exchange_id, direction="received", contains="ticker", limit=20,
)
```

`limit` 取**最新**的一段；单帧超 64 KiB 截断并标 `truncated`，单连接最多保留 500 帧（丢最早的），要留证及时读取或导 HAR。

## SSE 消息

`text/event-stream`（SSE）连接常年不关闭，`loadingFinished` 往往不触发，`read_network_body` 读不到内容——LLM 流式对话、服务端通知推送这类接口用 `read_sse_messages`。消息在 `eventSourceMessageReceived` 事件里逐条记录，无需等连接结束：

```python
traffic = await toolkit.inspect_network_traffic(resource_types=["EventSource"])
exchange_id = traffic.data["exchanges"][-1]["exchange_id"]

messages = await toolkit.read_sse_messages(exchange_id)
for message in messages.data["messages"]:
    message["event"], message["event_id"], message["data"]
    message.get("json")          # 消息正文是 JSON 时自动解析

messages.data["events"]          # {"message": 40, "chunk": 3, "done": 1}
messages.data["message_count"]

# 按事件名或子串过滤，limit 取最新的一段
deltas = await toolkit.read_sse_messages(exchange_id, event_name="chunk", limit=50)
```

单条消息超 64 KiB 截断并标 `truncated`，单连接最多保留 500 条（丢最早的），与 WebSocket 共用这两个上限。消息正文只回给调用方进程，模型侧只拿事件名分布与字节统计。用 `inspect_network_traffic` 定位 SSE 交换时，其 `resource_type` 为 `EventSource`。

## 接口契约剖析

把"页面上点出来的数据"变成"直接调接口拿到的数据"。`analyze_api_endpoint` 把同一接口的多次交换合并归纳——必须多次，因为单次调用分不清"递增的 page"和"恒定的 size"，而这正是能不能写出翻页代码的分界：

```python
api = await toolkit.analyze_api_endpoint(url_contains="/api/orders")
data = api.data
data["endpoint"]["url_template"]        # https://shop.example/api/orders/{id}
data["query_params"]                    # 每项含 name、role、value_type、samples、varies
data["auth"]["authorization_schemes"]   # ["Bearer"]
data["auth"]["cookie_names"]            # 只有名称，没有值
data["request_body"]["schema"]          # JSON 结构；GraphQL 另有 graphql 字段
data["response"]["record_path"]         # ["data", "list"] —— 批量数据在这里
data["response"]["total_fields"]        # ["total"]
data["pagination"]["strategy"]          # page_number / offset / cursor / none
```

`role` 把参数分成 `pagination` / `sort` / `timestamp` / `credential` / `filter`；`varies` 区分真入参和固定常量。

## 代码导出

```python
code = await toolkit.export_request_code(exchange_id, target="python_requests")
code.data["code"]                # 可直接运行的代码文本
code.data["placeholders"]        # [{"header": "Authorization", "env": "AUTHORIZATION"}]

runnable = await toolkit.export_request_code(
    exchange_id, target="curl", include_secrets=True,   # 明文凭据，仅受控环境
)
```

`target` 可选 `curl`、`python_requests`、`python_httpx`、`javascript_fetch`、`node_axios`。凭据默认替换为环境变量占位；`Content-Length`/`Host`/`Connection` 与 HTTP/2 伪 Header 会被丢弃——照抄浏览器的值只会让请求发不出去；正文按 `Content-Type` 选择正确传参方式（`json=` / `data=` / 原文）。

## 分页取全

`analyze_api_endpoint` 告诉你怎么翻页，`collect_api_pages` 替你把页翻完。页面上只要出现过一次该接口的请求就能用：

```python
pages = await toolkit.collect_api_pages(url_contains="/api/orders")
pages.success                 # 未取全时为 False
pages.data["closed"]          # 是否有正面证据证明取全
pages.data["reason"]          # 判据或缺口说明
pages.data["collected"]       # 87
pages.data["declared_total"]  # 服务端自己声明的总数
pages.data["records"]         # 全部记录，按页序拼接并去重
pages.data["plan"]            # 实际使用的策略、参数名、起点、每页大小
```

策略缺省从契约推断，可逐项覆盖：

```python
await toolkit.collect_api_pages(
    url_contains="/api/feed",
    strategy="cursor",
    page_param="cursor",
    cursor_field="next",          # 从响应哪个字段取下一个游标
    record_path=["data", "items"],
    dedupe_key="id",
    max_pages=100,
    delay_ms=200,                 # 页间间隔，避让速率限制
)
```

**页码在 POST 请求体里**（企业接口常见）传 `page_in="body"`。JSON 体与表单体都支持，嵌套字段用点号表示路径，字段原有类型会保留（服务端要数字却收到字符串通常直接 400）：

```python
# 来源请求体形如 {"query": {"pageNum": 1, "pageSize": 20}, "status": "paid"}
await toolkit.collect_api_pages(
    url_contains="/api/order/search",
    page_in="body",
    page_param="query.pageNum",   # 省略时按字段名自动猜，包括嵌套字段
)
```

URL 每页保持原样，翻页只体现在请求体上；`status=paid` 这类过滤条件原样带上。GET/HEAD 请求不能用 `page_in="body"`——浏览器不会给它们带请求体，工具会直接拒绝而不是让你抓到一堆重复的第一页。

**游标在响应头里**分两种。服务端给 GitHub 式 `Link: <…>; rel="next"` 时用 `cursor_in="link"`，下一页 URL 直接采信服务端给的，本地不拼任何分页参数：

```python
await toolkit.collect_api_pages(url_contains="/api/items", cursor_in="link")
```

游标在自定义响应头里时用 `cursor_in="header"` 指明头名，游标仍按 `page_param` 送回查询串：

```python
await toolkit.collect_api_pages(
    url_contains="/api/items",
    cursor_in="header",
    cursor_header="X-Next-Cursor",
    page_param="cursor",
)
```

两种模式都以"服务端不再给下一页"作为走到尽头的判据。指定 `cursor_in` 就等于声明这是游标分页，不必再传 `strategy`。

三条纪律：

1. **闭合结论要当真**：`closed=True` 只在收齐数等于声明总数、或末页短于整页时给出；跑到 `max_pages` 或任何一页失败一律 `closed=False` 并报缺口条数，已抓记录仍交还，可调大 `max_pages` 续采。
2. **服务端忽略分页参数会被识破**：参数名猜错时服务端照返第一页，判据是"这一页有没有新记录"，零新增即停并在 `failed_pages` 点明。遇到就显式指定 `page_param`。
3. **起点沿用样本 URL 自己的取值**：页码从 0 还是 1 起因接口而异，猜错漏首页；需要时用 `start` 覆盖。

每页复用来源请求的 Header 与会话 Cookie，登录态与签名参数跟着走；目标 origin 受任务授权约束。

## 路由改写

拦截浏览器**将要发起**的请求（不生成新请求）：`block` / `modify_request` / `mock_response` / `modify_response`，每域名最多 8 条：

```python
await toolkit.manage_network_route(
    "add", rule_id="mock-price", url_pattern="/api/price", action="mock_response",
    response_status=200, response_body='{"price": 1}',
)
rules = (await toolkit.manage_network_route("list")).data["rules"]
await toolkit.manage_network_route("remove", rule_id="mock-price")
```

规则用完必须显式撤销，否则会一直影响后续页面。敏感 Header 不能写进 `request_headers`，走 `request_header_input_keys` 映射到 `inputs` 键名：

```python
await toolkit.manage_network_route(
    "add", rule_id="auth-probe", url_pattern="/api/orders", action="modify_request",
    request_headers={"X-Trace": "probe-1"},
    request_header_input_keys={"Authorization": "token"},
)
```

# 会话与环境深指南：文件、Cookie、会话态、模拟、性能、脚本导出

目录：文件上传与下载 → Cookie 与 Web Storage → 会话态整体存取 → 环境模拟 → PDF 导出 → 性能采集 → 动作脚本导出 → 诊断与能力缺口。

## 文件上传与下载

上传走 CDP 注入，不打开系统文件对话框。路径必须是已存在的绝对路径——相对路径、目录、不存在的文件在触碰浏览器前就被拒绝（CDP 会静默接受坏路径造出空文件，所以校验必须前置）：

```python
await toolkit.upload_files(
    locator={"strategy": "css", "value": "input[type=file]"},
    paths=["/abs/path/invoice.pdf"],
    expect_kind="text_contains", expect_value="invoice.pdf",
)
await toolkit.upload_files(
    locator={"strategy": "css", "value": "#multi"},
    path_input_keys=["invoice_path", "attachment_path"],   # 路径也可走任务输入
)
```

下载在浏览器启动时已接管到任务产物目录 `downloads/`。先挂等待再触发，避免竞态：

```python
waiting = asyncio.create_task(
    toolkit.wait_for_download(suggested_filename="report.csv", timeout_seconds=30)
)
await toolkit.click_locator(
    {"strategy": "css", "value": "a.download"},
    expect_kind="text_contains", expect_value="正在导出",
)
done = (await waiting).data
done["path"]                 # 可读文件名副本，0600
done["raw_path"]             # Chrome 以 GUID 命名的原始落盘路径

downloads = (await toolkit.list_downloads()).data["downloads"]
```

## Cookie 与 Web Storage

纯 CDP 协议：不要求页面在前台，headless 与后台标签页都能执行，不会把页面切到前台。

```python
cookies = (await toolkit.read_cookies()).data["cookies"]   # 调用方拿完整值
await toolkit.set_cookie(
    "sid",
    value_input_key="session_token",   # 敏感值放 inputs，不写进工具参数
)
await toolkit.read_web_storage("local")                    # 省略 key 时只列键名
await toolkit.read_web_storage("local", key="theme")
await toolkit.write_web_storage("local", "theme", value="dark")
await toolkit.write_web_storage("session", "draft", remove=True)
```

写入 URL 默认取当前页，且必须在任务授权 origin 内；iframe 内的存储用 `frame_id`（来自 `list_frames`）指定作用域。

## 会话态整体存取（跳过重复登录）

快照结构与 Playwright 的 `storageState` 一致，两边可以互喂：

```python
exported = await toolkit.manage_storage_state("export")
exported.data["file_path"]   # 0600 私有文件，含会话凭据——泄漏等同账号泄漏
exported.data["state"]       # 同样内容，可自行持久化

# 换一个浏览器/进程后
await toolkit.manage_storage_state("import", file_path=exported.data["file_path"])
await toolkit.navigate(f"{base}/home")   # 直接进受保护页面，不再登录
```

导入时越出任务授权 origin 的 Cookie 会被跳过并列出；Web Storage 只能写进当前页面自身 origin，其余记在 `origins_skipped`，切到该页面后再导入一次。导入不校验快照是否过期——服务端已失效的会话导入后仍会被重定向回登录页。

## 环境模拟

移动端站点按 UA 与视口返回**完全不同的 DOM**，验证移动版必须先切设备、再重新导航：

```python
await toolkit.emulate_environment(device="iphone_15")   # 视口 + UA + 客户端提示 + 触控
await toolkit.navigate(url)                             # 服务端按 UA 分流，切完必须重新导航

await toolkit.emulate_environment(network_preset="slow_3g", cpu_throttle_rate=4)
await toolkit.emulate_environment(color_scheme="dark", timezone="Asia/Tokyo")
await toolkit.emulate_environment(geolocation={"latitude": 35.68, "longitude": 139.76})
await toolkit.emulate_environment(reset=True)           # 清除全部覆盖
```

设备预设：`iphone_15`、`pixel_8`、`ipad_air`、`desktop_1080p`；网络预设：`offline`、`slow_3g`、`fast_3g`、`regular_4g`、`no_throttle`。各维度独立叠加，只传要改的。

三条实测注意事项：

- **请求视口宽不等于生效宽。** 页面缺 `viewport` meta 时移动视口会退回 980 CSS 像素默认布局宽。看返回值 `data["effective"]["innerWidth"]` 而不是自己传的数；不一致时 `message` 会写明"请求宽度未生效"。
- **新标签页不继承覆盖**，工具会在切页时自动重施；有模拟生效时 `open_tab` 先建空白页再导航，避免请求赶在覆盖生效前发出。
- `device` 与 `viewport` 同时给出时，尺寸以 `viewport` 为准，UA 与触控仍来自 `device`。

## PDF 导出

```python
result = await toolkit.save_pdf(label="对账单", paper="a4", page_ranges="1-3")
result.data["pdf_path"]   # 0600 私有文件
```

支持 `paper`（a3/a4/a5/letter/legal/tabloid）、`landscape`、`scale`、`margin_inches`、`print_background`、`prefer_css_page_size`。渲染的是打印样式：`@media print` 隐藏的内容不会出现，懒加载图片未滚动到过就不会被打印。

## 性能采集

```python
result = await toolkit.measure_performance(reload=True)
result.data["core_web_vitals"]   # lcp_ms / fcp_ms / cls / inp_ms / ttfb_ms
result.data["ratings"]           # 按 Google 公开阈值：good / needs_improvement / poor
result.data["resources"]["slowest"]
result.data["counters"]          # DOM 节点数、布局对象数、事件监听器数
```

**要测 LCP 必须 `reload=True`**：采集器只有早于导航安装才能观察到最大内容绘制；导航后再装，FCP 能从缓冲区补回，LCP 只会是 `null`。`reload=False` 时 `message` 会明说 LCP 为什么缺席，不会悄悄返回 0。注意 `reload=True` 会真实重载页面，未提交的表单内容会丢。

## 动作脚本导出

流程跑通后，把已验证动作固化成可独立重跑的 Python 脚本：

```python
exported = await toolkit.export_action_script()
script = exported.data["code"]
Path("replay.py").write_text(script, encoding="utf-8")

exported.data["input_keys"]           # 脚本引用到的任务输入键
exported.data["needs_manual_review"]  # 需要人工补定位器的步骤
```

只有成功执行并通过业务后置校验的页面动作进入脚本。观察候选的 `target_id` 带会话版本号、跨会话必然失效，导出时会用当时命中的候选反推稳定定位器，优先级：`data-testid` → CSS `#id` → pointer 选择器 → role+name → `[name=…]` → 可见文本；都取不到时保留原值并标进 `needs_manual_review`。敏感值继续以输入键引用，脚本里只有 `INPUTS = {"account": ""}` 占位。脚本的浏览器配置走本地配置与环境变量（`WITTY_BROWSER_AUTO_HEADLESS` 等），换无头或换 profile 不用改代码。

## 诊断与能力缺口

动作没产生预期结果、页面行为不明时，先看诊断再决定下一步：

```python
await toolkit.inspect_page_diagnostics()     # 页面就绪/焦点、控制台异常、失败请求（有界脱敏）
```

现有工具表达不了目标时记录结构化缺口（只记录，不改代码不重启）：

```python
await toolkit.report_capability_gap(
    area="browser_action", capability="iframe 内元素定位",
    evidence="目标输入框位于跨域 iframe，当前定位器无法命中",
)
```

# 复杂交互深指南：iframe、拖拽、视觉动作、元素读取、对话框、标签页

目录：iframe → 元素读取与元素截图 → 拖拽（三种）→ 视觉动作与验证码 → 原生对话框 → 标签页。

## iframe

定位器默认只在主框架查找，不穿透任何 iframe。登录框、支付控件、验证码经常在 iframe 里，主框架定位必然找不到。先列帧再把 `frame_id` 放进定位器：

```python
frames = (await toolkit.list_frames()).data["frames"]
payment = next(f for f in frames if "payment" in f["url"])
await toolkit.input_text_locator(
    {"strategy": "css", "value": "#card-number", "frame_id": payment["frame_id"]},
    input_key="card_number",
)
await toolkit.click_locator(
    {"strategy": "role", "value": "button", "name": "确认支付",
     "frame_id": payment["frame_id"]},
    expect_kind="text_contains", expect_value="支付成功",
)
```

规则：

- `frame_id` 在所有接受 `locator` 的工具上可用，包括 `read_element` 与 `press_key`。
- `cross_origin` 为真的帧在独立渲染进程里，工具箱自动接管并换算坐标，调用方无需区别对待。
- 带 `frame_id` 的动作，`text_contains` 后置条件在同一帧里校验，可直接用帧内文案验收；`url_contains` / `title_contains` 始终指顶层页面。
- `frame_id` 只在当前页面有效，换页或切标签后必须重新 `list_frames()`。
- 帧内 `css`/`xpath` 走标准 DOM 查询语义，不穿透 user-agent shadow DOM；主框架定位会穿透。
- `observe()` 的语义候选只覆盖主框架，iframe 内元素拿不到 `target_id`，只能用定位器。

## 元素读取与元素截图（都是只读，不计动作、不作废观察）

写动作之前先确认面对的是不是目标元素：

```python
state = (await toolkit.read_element("username-input")).data
state["tag"], state["role"], state["name"]                    # 语义
state["visible"], state["in_viewport"], state["disabled"], state["box"]
state["text"], state["text_truncated"], state["text_length"]  # 文本按上限截断
state["value"], state["value_length"], state["value_masked"]  # 表单控件才有
state["options"], state["option_count"]                       # select 才有

await toolkit.read_element("order-table", max_text_length=8000, include_html=True)
```

`target_id` 与 `locator` 二选一。密码框 `value` 恒为 `None` 且 `value_masked` 为真；隐藏与禁用元素也能读，用来解释"为什么点不动"。

```python
shot = (await toolkit.capture_element_screenshot("captcha-image", label="验证码")).data
shot["screenshot_path"]      # PNG，0600
shot["box"], shot["clip"]

await toolkit.capture_element_screenshot(
    locator={"strategy": "css", "value": "#chart"}, padding=12,
)
```

元素在视口外也能截到，工具不滚动页面（不打断页面上正在进行的交互）；`padding` 上限 200 像素。

## 拖拽：三种工具，三种场景

**`drag_to_element`：元素拖到元素**（排序、看板换列、拖进文件夹）。页面用 HTML5 原生拖放还是鼠标事件由工具自动识别——两条通道不能混用：纯鼠标事件对 `draggable="true"` 的元素只触发 `dragstart`，`drop` 永远不发生，表现为"看着做了、其实没放下"。

```python
await toolkit.drag_to_element(
    source_locator={"strategy": "test_id", "value": "card-3"},
    target_locator={"strategy": "css", "value": "#column-done"},
)
await toolkit.drag_to_element(source_target_id=row_id, target_target_id=folder_id)
```

看板卡片多半是普通 `div`，不进语义候选（实测整页 `observe()` 候选数为 0），所以这类界面上通常只能用定位器。返回值 `data["channel"]` 告诉你走了 `html5` 还是 `pointer` 通道。

**`drag`：从目标中心按相对位移拖**（业务滑块、区间选择）。安全挑战必须显式声明：

```python
await toolkit.drag(
    "slider-handle", end_dx=260, end_dy=0, duration_ms=900, steps=24,
    security_challenge=True, expect_kind="text_contains", expect_value="验证通过",
)
```

**`visual_drag`：按截图比例坐标拖**（语义候选完全没有目标时的兜底），见下节。

安全挑战滑块不允许走 `drag_to_element`，工具会拒绝并要求改用 `drag` 或 `visual_drag`——那里才有截图留证与尝试预算。

## 视觉动作与验证码

需要 `launch_browser_toolkit(..., allow_visual_actions=True)`。坐标用 0-1 比例，必须绑定截图指纹与置信度，让执行层把动作和它的视觉依据绑在一起：

```python
await toolkit.visual_click(
    screenshot_fingerprint=shot_fp, x_ratio=0.52, y_ratio=0.38,
    visual_confidence=0.9, expect_kind="url_contains", expect_value="/detail",
)
await toolkit.visual_drag(
    screenshot_fingerprint=shot_fp,
    start_x_ratio=0.31, start_y_ratio=0.62, end_x_ratio=0.78, end_y_ratio=0.62,
    duration_ms=1100, steps=28, visual_confidence=0.86, security_challenge=True,
    expect_kind="text_contains", expect_value="验证通过",
)
```

验证码太小看不清时先放大局部（只读）：

```python
await toolkit.inspect_visual_region(
    screenshot_fingerprint=shot_fp, x_ratio=0.4, y_ratio=0.3,
    width_ratio=0.2, height_ratio=0.1, visual_confidence=0.9,
)
```

图形验证码识别出的文本用 `input_generated_text` 写回（不得用于账号密码——那些走任务输入）：

```python
await toolkit.input_generated_text(
    "captcha-input", text="8F3D", screenshot_fingerprint=shot_fp,
    visual_confidence=0.82, security_challenge=True,
)
```

## 原生对话框

`alert` / `confirm` / `prompt` / `beforeunload` 会挂起渲染进程直到被应答，因此工具在弹出瞬间自动回答，不会等你决定。`handle_dialog` 设置的是**下一次或后续**怎么答：

```python
await toolkit.handle_dialog("accept", scope="next")     # 只放行下一个弹窗
await toolkit.click(delete_button, expect_kind="text_contains", expect_value="已删除")

await toolkit.handle_dialog(
    "accept", scope="next", dialog_kinds=["prompt"],
    prompt_text_input_key="reason",                     # 敏感值走任务输入键
)
state = await toolkit.handle_dialog("inspect")          # 只读查看策略与已接管记录
state.data["dialogs"]
```

默认策略按"不替调用方做不可逆决定"选取：`confirm` / `prompt` 背后通常是删除、覆盖、提交，默认取消；`alert` 只有一个按钮；`beforeunload` 拦下的是你自己刚要求的导航，默认确认。要确认一次不可逆操作，用 `scope="next"` 精确放行那一次，别改成 `session`——那会把后面所有确认框都点掉。

## 标签页

```python
tabs = (await toolkit.list_tabs()).data["tabs"]       # 脱敏 URL、标题、是否当前页
new_tab = (await toolkit.open_tab("https://example.com/detail")).data
await toolkit.switch_tab(tabs[1]["target_id"])
await toolkit.close_tab(new_tab["target_id"])
```

`open_tab` 与 `navigate` 走同一条授权域名判定；新页由任务自有可以关闭，用户原有页面无论如何不会被关。打开或切换后旧观察全部作废，必须重新 `observe()`。

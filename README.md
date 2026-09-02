# Witty 浏览器工具库

面向大模型智能体的确定性浏览器工具库，直接基于 Chrome DevTools Protocol（CDP）实现，不使用 Playwright、Selenium 或 DrissionPage 运行时。

本仓库**不包含任何大模型调用**：由你（外部智能体或脚本）决定下一步，本库负责把每一步做对——参数校验、业务后置条件、脱敏、非幂等防重放和安全挑战约束全部由确定性代码保证。

## 快速上手

```bash
uv sync --extra dev
uv run witty-browser-auto doctor   # 自检浏览器可执行文件与存储目录
```

```python
import asyncio
from witty_browser_auto.toolkit import launch_browser_toolkit

async def main() -> None:
    async with launch_browser_toolkit("https://example.com/orders") as toolkit:
        # 先试重放：同场景跑过一次的采集直接零决策重放
        replay = await toolkit.replay_collection_program()
        if replay.success:
            print(replay.evidence[0].path)  # 私有 JSON 产物
            return
        # 首次运行或站点改版：检查结构 + 提交规格，成功后自动晋升为可重放程序
        inspection = await toolkit.inspect_collection_structure()
        result = await toolkit.run_structured_extraction(
            collection_name="订单列表",
            candidate_id=inspection.data["candidates"][0]["candidate_id"],
        )
        print(result.data)

asyncio.run(main())
```

完整用法（64 个开放工具的调用示例、敏感输入约定、iframe/上传下载/流量检查/接口逆向/分页采集等）见 `skills/use-browser-toolkit/SKILL.md`——这份文档就是给大模型智能体读的。

## 两种接入方式

| 你的智能体 | 接入方式 |
| --- | --- |
| 能执行 Python（Cursor、Claude Code、Codex 等编码型） | 直接 `import witty_browser_auto.toolkit`，读 `SKILL.md`；产出物是可脱离模型反复运行的脚本 |
| 不能执行代码，或不是 Python（Claude Desktop、各类 harness） | MCP 服务端：`witty-browser-auto mcp`（stdio，协议 `2025-06-18`） |

```bash
# MCP：core 档位暴露 25 个主线工具 + 3 个会话工具；all 档位暴露全部开放工具
witty-browser-auto mcp --profile core --allow-origin https://example.com --input-file ./inputs.json
```

MCP 侧的调用顺序是 `open_browser` → `observe` → 各类工具 → `close_browser`。`observe` 返回候选与 `target_id`（元素类工具必须逐字引用）；每个页面动作的结果自带 `page`——动作后的新观察——所以一个智能体步只需一次工具调用，不必再 `observe`。`click` 等动作可以不给后置条件，缺省按"页面有变化"校验。敏感值经 `--input`/`--input-file` 注入，工具参数只写键名；非敏感字面量直接给 `input_text` 的 `text`。

把观察与工具结果送进你自己的模型时用这三个转换函数，它们同时承担 token 预算：

```python
from witty_browser_auto import observation_to_prompt, observation_to_dict, tool_result_to_dict, tool_schemas

tool_schemas()                       # OpenAI 兼容工具定义，默认已排除终态工具
observation_to_prompt(observation)   # 紧凑文本，逐字列出 target_id
tool_result_to_dict(result)          # 保留 failure_kind 与后置条件结论
```

## 能力总览

- **页面观察与定位**：AX+DOM 候选（可输入控件 > 控件 > 链接、视口内优先的稳定次序）、CSS/XPath/role/text/label/test-id 显式定位、iframe 帧作用域、Shadow DOM 穿透。
- **页面操作**：点击（左/右/双击）、输入（敏感值键名引用 / 非敏感字面量）、选择、悬停、滚动、按键组合、表单批量填写、元素到元素双通道拖放、页面历史；每个动作结果自带动作后的新观察，一步一调。
- **结构化采集**：只读结构分析 + 确定性整页采集（页码/下一页/加载更多/无限滚动/逐条详情），完整性门要求声明总数或稳定终点闭合；成功规格经"重进入口 + 结构探针"验证门晋升为**可重放采集程序**，同场景第二次采集零决策重放，站点改版自动失配回退并降权。
- **网络能力**：完整流量检查（全部资源类型、timing、initiator、WebSocket 帧）、HAR 导出、请求重放、接口契约剖析、curl/requests/httpx/fetch/axios 代码导出、沿 page/offset/cursor 三策略的主动分页采集（强制闭合证据）。
- **会话与状态**：Cookie/Web Storage 受控读写、Playwright 兼容的会话态整体导出导入、文件上传、下载接管。
- **环境与诊断**：设备/网络/时区/地理模拟、原生对话框接管、页面性能采集、CDP 页面诊断、元素与区域截图。
- **脚本固化**：已验证动作导出为可独立重跑的 Python 脚本（`export_action_script`），会话内 target_id 自动反推为跨会话稳定定位器。
- **双轨安全执行**：探索和一次性决策由调用方负责，成功动作自动沉淀为确定性脚本；生产环境可用 `read_only=True`、`security.read_only=true` 或 `WITTY_BROWSER_AUTO_READ_ONLY=true` 硬禁点击、输入、上传、存储写入与请求重放。
- **隐私纪律**：敏感值经 `inputs` 键名引用、日志与事件全链路脱敏、业务数据只落私有 0600 文件不进调用方摘要。

## 环境

- Python 3.11+，运行依赖仅 `aiohttp`
- Chrome 或 Chromium（受管模式自动启动；`takeover` 模式接管已授权的日常 Chrome）

## 配置

零配置可用：默认自动查找本机 Chrome，存储落在用户目录。需要定制时写 `.witty-browser-auto/config.json`（`0600`）或环境变量：

- `WITTY_BROWSER_AUTO_BROWSER_EXECUTABLE`：Chrome/Chromium 可执行文件
- `WITTY_BROWSER_AUTO_CDP_ENDPOINT`：显式接管的本机 CDP 地址
- `WITTY_BROWSER_AUTO_BROWSER_SESSION_MODE`：`managed`（默认，专用浏览器）或 `takeover`（接管当前 Chrome）
- `WITTY_BROWSER_AUTO_HEADLESS`：无头模式，默认 `false`
- `WITTY_BROWSER_AUTO_READ_ONLY`：生产只读硬门控，默认 `false`
- `WITTY_BROWSER_AUTO_MEMORY_DB` / `WITTY_BROWSER_AUTO_ARTIFACT_ROOT`：记忆库与产物目录

配置优先级：`代码默认值 < 本地 JSON < 环境变量 < 调用方显式传入的 AppConfig`。

## 项目结构

```
src/witty_browser_auto/
  toolkit/     对外入口：工具目录、参数校验、BrowserToolkit 门面、序列化、一步装配
  mcp_server/  MCP stdio 服务端：协议帧、工具档位、会话生命周期
  agent/       确定性工具执行层（ToolExecutor 与各工具族，无模型调用）
  browser/     CDP 驱动：会话、定位、输入、表单、对话框、模拟、下载……
  cdp/         自研 CDP WebSocket 传输与协议层
  network/     流量日志、HAR、重放、契约剖析、代码导出、分页采集
  memory/      SQLite 记忆与已验证采集程序库
  domain/      领域模型与协议
  security/    脱敏
skills/use-browser-toolkit/SKILL.md   给外部智能体的完整使用文档
```

真实完成范围和延期项以 `docs/PROJECT_STATUS.md` 为准；每轮变更的动机与验证记录在 `docs/change_maintenance/CHANGELOG.md`。

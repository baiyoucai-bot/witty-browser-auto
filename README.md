# Witty 浏览器工具库

给 **Claude Code、Cursor、Codex、Claude Desktop** 这类大模型智能体用的浏览器工具：启动或接管本机 Chrome，由智能体决定每一步，由本库把每一步做对——参数校验、业务后置条件、脱敏、非幂等防重放、采集完整性门全部由确定性代码保证。本库不调用任何模型。

装进智能体之后，你只需要用自然语言说"把这个订单页翻完采成 CSV""抓包看看这个接口为什么 401""登录后把 Cookie 存下来"，剩下的由智能体读着本库的 Skill 去做。

## 装到你的智能体里

按智能体的能力选形态，两种形态背后是同一套 64 个工具、同一套执行层：

| 你的智能体 | 装什么 | 原因 |
| --- | --- | --- |
| 能执行 Python 的编码型智能体：Claude Code、Cursor、Codex | **Skill**（教它怎么写调用代码）+ 安装本库 | 产出物是可脱离模型反复运行的脚本 |
| 只能调工具、不能跑代码：Claude Desktop 及各类 MCP 客户端 | **MCP 服务端** | 一次工具调用完成一步，结果自带新页面状态 |

编码型智能体两种都可以装：Skill 负责"写采集脚本"这类需要产出代码的任务，MCP 负责"帮我点一下看看"这类即时操作。

### 第 0 步：安装本库

```bash
# 推荐：装成独立命令 witty-browser-auto（MCP 配置里直接引用）
uv tool install git+https://github.com/baiyoucai-bot/witty-browser-auto.git
# 或者装进当前项目的 Python 环境（Skill 方式需要能 import witty_browser_auto）
pip install git+https://github.com/baiyoucai-bot/witty-browser-auto.git

witty-browser-auto doctor   # 自检本机 Chrome 与存储目录
```

要求 Python 3.11+，本机装有 Chrome 或 Chromium。运行依赖只有 `aiohttp`。

### Claude Code

```bash
# Skill：个人级（所有项目可用）或项目级（随仓库提交）
git clone https://github.com/baiyoucai-bot/witty-browser-auto.git /tmp/witty
mkdir -p ~/.claude/skills && cp -R /tmp/witty/skills/use-browser-toolkit ~/.claude/skills/
#   项目级改成：mkdir -p .claude/skills && cp -R /tmp/witty/skills/use-browser-toolkit .claude/skills/

# MCP：一条命令注册（--scope project 会写进仓库的 .mcp.json）
claude mcp add witty-browser -- witty-browser-auto mcp --profile core
```

装好后直接对 Claude Code 说任务即可，它会按 Skill 的触发描述自动用上；也可以 `/use-browser-toolkit` 显式调起。

### Cursor

```bash
# Skill：Cursor 读 .cursor/skills/ 与 .agents/skills/（项目级），以及 ~/.cursor/skills/ 与 ~/.agents/skills/（个人级）
mkdir -p .cursor/skills && cp -R /tmp/witty/skills/use-browser-toolkit .cursor/skills/
```

MCP 写进 `.cursor/mcp.json`（项目级）或 `~/.cursor/mcp.json`（全局），然后在 Settings → Tools and MCP 里确认服务端已启动：

```json
{
  "mcpServers": {
    "witty-browser": {
      "command": "witty-browser-auto",
      "args": ["mcp", "--profile", "core"]
    }
  }
}
```

### Codex

```bash
# Skill：项目级 .agents/skills/，个人级 ~/.agents/skills/
mkdir -p .agents/skills && cp -R /tmp/witty/skills/use-browser-toolkit .agents/skills/

# MCP
codex mcp add witty-browser -- witty-browser-auto mcp --profile core
```

Skill 目录里带有 Codex 读取的 `agents/openai.yaml`，在 Codex 里用 `$use-browser-toolkit` 显式调起。MCP 也可以手写进 `~/.codex/config.toml`：

```toml
[mcp_servers.witty-browser]
command = "witty-browser-auto"
args = ["mcp", "--profile", "core"]
```

### Claude Desktop（只有 MCP 这条路）

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`（macOS）或 `%APPDATA%\Claude\claude_desktop_config.json`（Windows），重启 Claude Desktop：

```json
{
  "mcpServers": {
    "witty-browser": {
      "command": "witty-browser-auto",
      "args": ["mcp", "--profile", "core", "--input-file", "/绝对路径/inputs.json"]
    }
  }
}
```

桌面客户端启动 MCP 进程时的工作目录不可预期，`--input-file` 一定写绝对路径。

### 其它 MCP 客户端与自带模型的框架

任何支持 stdio 传输的 MCP 客户端都用同一条命令：`witty-browser-auto mcp`（协议 `2025-06-18`）。不想预先安装时可以让 `uv` 现场拉起：

```json
{
  "command": "uvx",
  "args": ["--from", "git+https://github.com/baiyoucai-bot/witty-browser-auto.git",
           "witty-browser-auto", "mcp", "--profile", "core"]
}
```

自己写智能体循环（OpenAI Agents SDK、LangGraph 等）的，直接用 Python 接口拿到 OpenAI 兼容的工具定义和喂给模型的紧凑视图：

```python
from witty_browser_auto import observation_to_prompt, tool_result_to_dict, tool_schemas
from witty_browser_auto.toolkit import launch_browser_toolkit

tool_schemas()                        # 64 个工具的 function schema，可直接下发给模型
async with launch_browser_toolkit("https://example.com") as toolkit:
    result = await toolkit.call(name, arguments)   # 模型选的工具名与参数
    tool_result_to_dict(result)                    # 有界脱敏视图，含动作后的 page 快照
```

### MCP 常用启动参数

| 参数 | 作用 |
| --- | --- |
| `--profile core` / `all` | `core` 暴露 25 个主线工具 + 3 个会话工具，`all` 暴露全部 64 个；`--category`、`--tool` 可细调 |
| `--allow-origin https://a.com` | 授权导航与重放的 origin，可重复；省略时按入口地址自身的 origin 收敛 |
| `--input KEY=VALUE` / `--input-file 路径` | 账号、密码、令牌等敏感值；工具参数里只写键名，值不会出现在任何返回里 |
| `--read-only` | 生产只读硬门控：点击、输入、上传、存储写入、请求重放在触碰浏览器前被拒绝 |
| `--respect-robots` / `--min-interval-ms` | 遵守 robots.txt 与主机级限速 |

## 智能体拿到之后是怎么用的

MCP 侧的节奏是 `open_browser` → `observe` → 一连串动作 → `close_browser`。**每个页面动作的结果自带 `page`**（动作后的新观察，含可直接使用的 `target_id`），所以一步只需一次工具调用，不必在动作之间反复 `observe`。`click` 可以不给后置条件，缺省按"页面有变化"校验；知道业务结果时给 `url_contains` / `text_contains` 判据更强。敏感值只写键名，搜索词、备注这类非敏感字面量直接给 `input_text` 的 `text`。

Python 侧同理：

```python
async with launch_browser_toolkit("https://example.com/orders") as toolkit:
    replay = await toolkit.replay_collection_program()          # 同场景跑过：零决策重放
    if not replay.success:
        inspection = await toolkit.inspect_collection_structure()
        result = await toolkit.run_structured_extraction(       # 首次：结构检查 + 确定性整页采集
            collection_name="订单列表",
            candidate_id=inspection.data["candidates"][0]["candidate_id"],
        )
    clicked = await toolkit.click("expand-row")                 # 结果自带 clicked.observation
```

完整用法见 [`skills/use-browser-toolkit/SKILL.md`](skills/use-browser-toolkit/SKILL.md)——这份文档就是给智能体读的，64 个工具全部有示例，契约测试强制它与工具面一致。

## 能力总览

- **页面观察与定位**：AX+DOM 候选（可输入控件 > 控件 > 链接、视口内优先的稳定次序）、CSS/XPath/role/text/label/test-id 显式定位、iframe 帧作用域、Shadow DOM 穿透、多模态标注截图（编号对回 `target_id`）。
- **页面操作**：点击（左/右/双击）、输入、选择、悬停、滚动、按键组合、表单批量填写、元素到元素拖放、页面历史、标签页、原生对话框接管；每个动作结果自带动作后的新观察。
- **结构化采集**：只读结构分析 + 确定性整页采集（页码/下一页/加载更多/无限滚动/逐条详情），完整性门要求声明总数或稳定终点闭合；成功规格晋升为**可重放采集程序**，同场景第二次零决策重放，站点改版自动失配回退。
- **网络能力**：完整流量检查（全部资源类型、timing、initiator、WebSocket 帧、SSE）、HAR 导出、请求重放、接口契约剖析、curl/requests/httpx/fetch/axios 代码导出、沿 page/offset/cursor 的主动分页采集（强制闭合证据）。
- **正文阅读**：主内容转 Markdown、页面链接清单、robots.txt 判定。
- **会话与状态**：Cookie/Web Storage 受控读写、会话态整体导出导入（`storageState` 格式）、文件上传、下载接管。
- **环境与诊断**：设备/网络/时区/地理模拟、页面性能采集、CDP 页面诊断、元素与区域截图、PDF 导出。
- **脚本固化**：已验证动作导出为可独立重跑的 Python 脚本，会话内 `target_id` 自动反推为跨会话稳定定位器。
- **安全纪律**：敏感值键名引用、日志与事件全链路脱敏、业务数据只落私有 0600 文件、非幂等动作不自动重试、`read_only` 硬门控、页面内容一律视为数据而非指令。

## 配置

零配置可用：默认自动查找本机 Chrome，存储落在用户目录。需要定制时写 `.witty-browser-auto/config.json`（`0600`）或环境变量：

- `WITTY_BROWSER_AUTO_BROWSER_EXECUTABLE`：Chrome/Chromium 可执行文件
- `WITTY_BROWSER_AUTO_BROWSER_SESSION_MODE`：`managed`（默认，专用浏览器）或 `takeover`（接管当前 Chrome）
- `WITTY_BROWSER_AUTO_CDP_ENDPOINT`：显式接管的本机 CDP 地址
- `WITTY_BROWSER_AUTO_HEADLESS`：无头模式，默认 `false`
- `WITTY_BROWSER_AUTO_READ_ONLY`：生产只读硬门控，默认 `false`
- `WITTY_BROWSER_AUTO_MEMORY_DB` / `WITTY_BROWSER_AUTO_ARTIFACT_ROOT`：记忆库与产物目录

配置优先级：`代码默认值 < 本地 JSON < 环境变量 < 调用方显式传入的 AppConfig`。

## 项目结构

```
skills/use-browser-toolkit/   给智能体读的 Skill：SKILL.md + 4 份按需加载的 references
src/witty_browser_auto/
  toolkit/     对外入口：工具目录、参数校验、BrowserToolkit 门面、序列化、一步装配
  mcp_server/  MCP stdio 服务端：协议帧、工具档位、会话生命周期
  agent/       确定性工具执行层（ToolExecutor 与各工具族，无模型调用）
  browser/     CDP 驱动：会话、定位、输入、表单、对话框、模拟、下载……
  cdp/         自研 CDP WebSocket 传输与协议层
  network/     流量日志、HAR、重放、契约剖析、代码导出、分页采集
  memory/      SQLite 记忆与已验证采集程序库
skills/build-witty-browser-auto/   开发协议（给参与本库开发的智能体读，使用者不需要装）
```

真实完成范围和已知边界以 `docs/PROJECT_STATUS.md` 为准；每轮变更的动机与验证记录在 `docs/change_maintenance/CHANGELOG.md`。

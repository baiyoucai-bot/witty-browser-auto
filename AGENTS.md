## 浏览器任务一律走本库，并且写成脚本

这个仓库就是 Witty 浏览器工具库本身。凡是用户要求**操作网页、采集数据、抓包、逆向接口、自动登录、翻页取全**，先读 `skills/use-browser-toolkit/SKILL.md`（`.agents/skills/` 与 `.claude/skills/` 下是指向它的符号链接，技能自动发现失败时直接按路径读），然后**写 Python 脚本**调用 `witty_browser_auto.toolkit` 去完成，运行脚本并把脚本与产物一起交付。不要改用其它浏览器自动化方案，也不要用一次性手动点击代替脚本——用户要的是能再跑一次的东西。

开始前先确认 `python -c "import witty_browser_auto"` 能过（本仓库内用 `.venv/bin/python`，或先 `uv sync`），再 `witty-browser-auto doctor` 确认本机 Chrome 可用。

参与本库自身开发时读 `skills/build-witty-browser-auto/SKILL.md`。

<!-- CODEGRAPH_START -->
## CodeGraph

本仓库已启用 CodeGraph。需要理解或定位代码时，必须先使用 CodeGraph，再考虑文本搜索或逐文件阅读：

- MCP 可用时，优先调用 `codegraph_explore`，一次获取相关符号源码与调用路径。
- MCP 不可用时，运行 `codegraph explore "<符号名或问题>"`。
- 修改代码后运行 `codegraph sync .` 同步索引，并用 `codegraph status .` 确认索引为最新状态。
- 如果仓库根目录不存在 `.codegraph/`，不要自行建立索引；是否启用由项目负责人决定。
<!-- CODEGRAPH_END -->

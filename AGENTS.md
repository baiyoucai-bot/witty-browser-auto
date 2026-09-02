<!-- CODEGRAPH_START -->
## CodeGraph

本仓库已启用 CodeGraph。需要理解或定位代码时，必须先使用 CodeGraph，再考虑文本搜索或逐文件阅读：

- MCP 可用时，优先调用 `codegraph_explore`，一次获取相关符号源码与调用路径。
- MCP 不可用时，运行 `codegraph explore "<符号名或问题>"`。
- 修改代码后运行 `codegraph sync .` 同步索引，并用 `codegraph status .` 确认索引为最新状态。
- 如果仓库根目录不存在 `.codegraph/`，不要自行建立索引；是否启用由项目负责人决定。
<!-- CODEGRAPH_END -->

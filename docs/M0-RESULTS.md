# M0：上游核实结果与当前验证状态

日期：2026-11-14 / 15。上游事实来自直接阅读两个仓库在下列提交处的源码。

安装方式更新（Windows 第二轮）：`@llmwiki/core` 的 npm 安装与版本查询均 E404，因此改为固定提交 `32da49bf97743431a8cb3ff46e0e31c7fc629b18` 的源码构建。项目 setup 克隆至 `vendor/src/llmwiki`，执行 `npm ci`、`npm run build --workspace=packages/core`；入口为 `packages/core/dist/mcp/bin.js`。CodeGraph 仍从 npm 安装。ripgrep 14.1.1 从官方 release 下载并校验 SHA-256，存放于 `vendor/bin`。下方云端“未验证”表保留为历史状态，最新真实结果见 VERIFY-RESULTS.md。

## 上游事实

### microsoft/llmwiki（提交 32da49b，`@llmwiki/core` 0.1.2，MIT）

- 运行时：Node >= 20。启动器 `llmwiki-mcp [wiki-root]`，即 `dist/mcp/bin.js`；根目录取第一个参数或环境变量 `LLMWIKI_ROOT`，必须是已存在的目录。
- wiki 根目录是 `.wiki`，内含 `raw/`、`wiki/entities|concepts|sources/`、`wiki/index.md`、`wiki/log.md`、`AGENTS.md`。
- 读工具及参数：`wiki_status()`、`wiki_query(query)`、`wiki_lint(category?)`、`wiki_list_pages()`、`wiki_list_sources()`、`wiki_read_page(path)`（相对 `wiki/` 的路径）、`wiki_read_index()`。
- `wiki_query` 返回 `{query, matches, results:[{title, path, score, excerpt}]}`。
- 写工具存在于同一个服务（`write-tools.ts`），因此网关用白名单只放行读工具。
- 没有"读取原始材料内容"的工具（`wiki_list_sources` 只列文件），`read_source` 由网关自己实现。
- frontmatter 约定：`sources`（指向摘要页）、摘要页上的 `source_path`（原始文件相对路径）、`updated`、`created`。溯源链按此实现。

### colbymchenry/codegraph（提交 f72c9d2，1.6.2，MIT）

- npm 包 `@colbymchenry/codegraph`，Node >= 20 且 < 25；也有自带运行时的独立安装脚本。
- 索引：`codegraph init [path] --yes --local-only`（非交互；`.codegraph/` 自带 `.gitignore` 忽略自身）、`codegraph index [path]`（全量重建）、`codegraph sync [path]`、`codegraph status [path] --json`。
- 索引目录必须位于项目根目录内；`CODEGRAPH_DIR` 只能改目录名，不能改到仓库外。
- MCP：`codegraph serve --mcp [--no-watch] [--no-telemetry]`。默认只暴露 `codegraph_explore(query, maxFiles?, projectPath?)`；`projectPath` 可指向任意已索引项目，因此网关用一个会话查询所有仓库。
- `codegraph_explore` 返回文本：每个文件段以 `` **`路径`** — 符号列表 `` 开头，源码行格式为 `行号<TAB>代码`。
- 默认开启匿名使用统计和每日更新检查；`DO_NOT_TRACK=1` 可同时关闭两者。网关启动它时已关闭。

## 已在云端 Linux 环境验证（Python 3.13，mcp 2.3.0，10 项测试通过）

- 连接管理，针对真实 MCP 协议的真实子进程：懒启动、12 个并发首次调用只启动 1 个进程、复用、调用超时后恢复、进程自杀后恢复、被外部 kill 后恢复、空闲释放后再次懒启动、关闭后子进程退出、白名单拦截写工具、程序不存在与缺少必需工具的报错。
- 通过真实 MCP 客户端驱动 `python -m kbw serve`：6 个工具齐全；知识搜索 → 页面 → 原始材料的溯源链；缺失的来源与更新时间被标记为缺失；路径穿越、绝对路径、符号链接逃逸均被拒绝；无索引时代码搜索降级为 ripgrep 并返回仓库、提交号、相对路径、行号。
- 路由、结果不足时的并行扩查、跨服务扩查、RRF 融合、精确缓存命中与源码变更后失效：用 CodeGraph 替身进程验证了网关侧逻辑。
- 命令行：`doctor`、`index`（知识部分）、`status`、`serve`、`stop`。

## 未验证（云端环境无法联网安装，必须在本机完成）

| 项目 | 状态 |
|---|---|
| 真实 LLMWiki MCP 调用 | 未验证 |
| 真实 CodeGraph 建索引与 MCP 调用，`parse_explore` 对真实输出的解析 | 未验证 |
| LLMWiki 固定提交源码构建与 `@colbymchenry/codegraph` 1.6.2 npm 安装 | 云端未验证；Windows 改用源码构建 LLMWiki，npm E404 证据保留于真实报告 |
| FastEmbed 加载 bge-small-zh-v1.5 / bge-reranker-base，`local_files_only` 参数 | 未验证 |
| sqlite-vec 向量表的建立与查询 | 未验证 |
| `setup` 命令全流程、`requirements.lock` 生成 | 未验证 |
| Windows：`start.ps1`、子进程清理、`stop`、路径处理 | 未验证 |
| Python 3.11 | 未验证（代码按 3.11 语法编写，测试跑在 3.13） |
| 离线安装流程 | 未验证 |
| 冷启动与热查询耗时 | 未记录，由 `scripts/verify_real.py` 在本机记录 |

本机验证完成后，结果写入 `docs/VERIFY-RESULTS.md`。

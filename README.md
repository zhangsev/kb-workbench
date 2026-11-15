# kb-workbench：本地知识与代码检索工作台

一个只读的 MCP 网关。Codex / Claude Code 只接它一个 MCP，就能同时检索知识页面和多个 Java 仓库的源码。

- 知识库下游：[microsoft/llmwiki](https://github.com/microsoft/llmwiki) 的 `@llmwiki/core`（stdio MCP，`llmwiki-mcp`）
- 代码下游：[colbymchenry/codegraph](https://github.com/colbymchenry/codegraph)（stdio MCP，`codegraph serve --mcp`）
- 网关自建：SQLite FTS5 全文索引、sqlite-vec 向量索引、ripgrep 精确搜索、RRF 融合、精确缓存

当前验证状态见 [docs/M0-RESULTS.md](docs/M0-RESULTS.md) 与 `docs/VERIFY-RESULTS.md`（真实验证后生成）。

## 1. 快速开始

Windows（PowerShell）：

```powershell
.\start.ps1 setup --examples   # 建 .venv，源码构建 LLMWiki core，安装 CodeGraph、本地 rg、模型及示例
.\start.ps1 index              # 给每个仓库建 CodeGraph 索引，并建知识索引
.\start.ps1 doctor             # 检查全部依赖，失败项会给出修复命令
.\.venv\Scripts\python.exe scripts\verify_real.py   # 真实下游验证，结果写入 docs\VERIFY-RESULTS.md
```

Linux：把 `.\start.ps1` 换成 `./start.sh`，python 路径换成 `.venv/bin/python`。

用自己的仓库：复制 `kbw.example.yaml` 为 `kbw.yaml`，按第 3 节填写，然后 `index`。之后一条命令启动：`.\start.ps1`（默认就是 `serve`）。

LLMWiki 不从 npm 安装（包返回 E404）。setup 将仓库克隆到 `vendor/src/llmwiki`，checkout 固定提交 `32da49bf97743431a8cb3ff46e0e31c7fc629b18`，执行 `npm ci` 和 `npm run build --workspace=packages/core`，不构建 VS Code 扩展。npm 缓存位于 `vendor/npm-cache`。ripgrep 14.1.1 从官方 GitHub release 下载、验证 SHA-256 后放入 `vendor/bin`；运行期优先使用此文件，其次使用 PATH，不更改系统 PATH。

## 2. 命令

| 命令 | 作用 | 是否联网 |
|---|---|---|
| `setup` | 安装 Python 依赖、源码构建 LLMWiki core、安装 CodeGraph 和本地 ripgrep、下载模型；`--examples` 生成示例；`--pack-offline` 额外下载离线 wheel | 只有它联网；`--offline` 时完全不联网 |
| `index` | 未索引的仓库执行 `codegraph init <路径> --yes`，已索引的执行 `codegraph sync`；`--rebuild` 全量重建；同时更新知识索引 | 否 |
| `serve` | 以 stdio 方式运行 MCP 网关。日志全部写 stderr，stdout 只有 MCP 协议 | 否 |
| `status` | 配置、知识索引、各仓库索引与新鲜度、模型、正在运行的网关进程；`--json` 输出完整信息 | 否 |
| `stop` | 停止由本配置启动的网关进程（核对命令行后才终止，不动 CodeGraph 自己的共享 daemon） | 否 |
| `doctor` | 检查运行时、上游程序、仓库索引、模型、数据库；失败项附修复命令 | 否 |

`start.ps1` / `start.sh` 只做检查并给出修复命令，自己不下载任何东西。

## 3. 你需要填写的最少配置

`kbw.yaml` 里只有三处必须改：

1. `knowledge.wiki_root`：LLM Wiki 的 `.wiki` 目录（里面有 `wiki/` 和 `raw/`）。
2. `knowledge.source_dirs`：允许 `read_source` 读取的原始材料目录，一般就是 `.wiki/raw`。
3. `repos`：每个仓库的 `name`、`path`、`package_prefixes`、`keywords`，以及 `depends_on`（它调用哪些服务）和 `topics`（收发的消息主题）。后两项决定跨服务问题会主动去查哪些仓库。

其余项都有默认值，完整说明见 `kbw.example.yaml`。

## 4. MCP 配置（可直接复制，把路径换成你的安装目录）

Codex，写入 `~/.codex/config.toml`：

```toml
[mcp_servers.kb-workbench]
command = 'D:\dev\project\kb-workbench\.venv\Scripts\python.exe'
args = ["-m", "kbw", "serve"]
env = { KBW_CONFIG = 'D:\dev\project\kb-workbench\kbw.yaml', PYTHONUTF8 = "1" }
```

Claude Code，项目根目录的 `.mcp.json`：

```json
{
  "mcpServers": {
    "kb-workbench": {
      "command": "D:\\dev\\project\\kb-workbench\\.venv\\Scripts\\python.exe",
      "args": ["-m", "kbw", "serve"],
      "env": { "KBW_CONFIG": "D:\\dev\\project\\kb-workbench\\kbw.yaml", "PYTHONUTF8": "1" }
    }
  }
}
```

Linux 下 `command` 为 `/opt/kb-workbench/.venv/bin/python`，`KBW_CONFIG` 为 `/opt/kb-workbench/kbw.yaml`。

网关只读。需要让 Agent 写知识页面时，另外直接接 LLMWiki 自己的 MCP（它带写工具）：

```json
{ "mcpServers": { "llmwiki": {
  "command": "node",
  "args": ["D:\\dev\\project\\kb-workbench\\vendor\\src\\llmwiki\\packages\\core\\dist\\mcp\\bin.js", "D:\\knowledge\\.wiki"]
} } }
```

## 5. 对外工具

| 工具 | 说明 |
|---|---|
| `search_knowledge(query, top_k)` | 全文 + 语义 + `wiki_query` 三路召回，按页面做 RRF 融合，可选重排 |
| `read_page(path)` | 读取知识页面（走 `wiki_read_page`，下游不可用时改读本地文件并标明） |
| `read_source(path, start_line, max_lines)` | 读取原始材料，只能读 `source_dirs` 内的文件 |
| `search_code(query, repos, top_k)` | 路由到候选仓库，查 CodeGraph，ripgrep 精确兜底，必要时扩查 |
| `search_all(query, top_k)` | 知识与代码一起查，两个排名再做一次 RRF |
| `service_status()` | 下游连接、各仓库索引状态、模型、缓存、降级原因 |

每个结果都带 `degraded` 列表，说明哪一路检索不可用以及原因。

## 6. 检索规则

**知识**：FTS5（中文按二字切分，标识符按驼峰拆分）、sqlite-vec（bge-small-zh）、下游 `wiki_query` 各出一个排名，只用名次做 RRF，不比较原始分数。查询前自动对照文件修改时间做增量索引。

**代码**：

1. 从问题里提取类名、方法名、接口路径、点分名称（包名、Topic、配置项）、常量名。
2. 给每个仓库打分：仓库名或包名前缀命中 +3，关键词或 Topic 命中 +2，ripgrep 确认该仓库确实含有某个符号每个 +2。
3. 先查第一名。结果不足（文件段数少于 `min_results`，或结果中不含任何已提取的符号）时，并行扩查前 `expand_top_n` 个候选。
4. 跨服务：两个以上仓库有路由得分，或问题含"下游、调用链、消费"等词时，主动查询 `depends_on`、被依赖方和共享 Topic 的仓库，不因第一仓库有结果而停止。
5. 所有涉及的仓库再用 ripgrep 做固定字符串精确搜索，作为独立一路参与 RRF。
6. 仓库未建索引或 CodeGraph 不可用时，只返回 ripgrep 结果并标明降级。

**不会做的事**：没有模型时不生成任何"假向量"，语义这一路直接缺席并在 `degraded` 中说明。

## 7. 溯源与新鲜度

- 代码结果：`repo`、`commit`（当前 HEAD）、`path`（仓库内相对路径）、`lines`、`index_freshness`。
  新鲜度取值：`fresh`、`uncommitted_changes`、`stale_head_moved`、`not_indexed`、`unknown`、`repo_path_missing`。
  判断依据是 `kbw index` 时记录的提交号与当前 HEAD 的比较。CodeGraph 自带文件监听，实际索引可能比这个判断更新。
- 知识结果：页面路径、行号、frontmatter 中的 `sources` → 摘要页的 `source_path` → 原始材料路径、`updated` 日期、文件修改时间。缺少的项列在 `provenance.missing` 里，不补造。
- `read_source` 先解析符号链接再判断是否位于允许目录内，路径穿越和链接逃逸都会被拒绝。

## 8. 缓存

只有精确缓存。键 = 工具名 + 规范化查询 + 检索范围 + 索引版本。索引版本包含每个仓库的 HEAD、未提交改动的摘要（含文件修改时间）、是否已索引，以及知识目录所有页面的修改时间与大小。任何一项变化都会自然不命中。带降级标记的结果不写入缓存。

## 9. 下游连接管理

- 首次调用才启动（懒启动）；并发的首次调用只启动一个进程。
- 长连接复用；每次调用有超时；超时或连接断开后丢弃连接，下次调用自动重连（连接断开时当次调用自动重试一次）。
- 空闲 `idle_seconds` 后释放。
- 关闭时只终止自己启动的子进程。CodeGraph 自行拉起的共享 daemon 不由网关管理，交给它自己的空闲超时。
- LLMWiki 只放行 6 个读工具，写工具在网关侧直接拒绝。
- 启动 CodeGraph 时设置 `DO_NOT_TRACK=1`、`CODEGRAPH_TELEMETRY=0`、`CODEGRAPH_NO_UPDATE_CHECK=1`，关闭其匿名统计和更新检查；npm 1.6.2 不接受 `--no-telemetry`。另设 `CODEGRAPH_NO_DOWNLOAD=1` 禁止运行时隐式下载。

## 10. 对源码仓库的写入

网关和 `status`、`doctor`、`serve` 不写仓库。`index` 会让 CodeGraph 在仓库根目录生成 `.codegraph/`；npm 1.6.2 不支持 `--local-only`，网关向仓库本地 Git `info/exclude` 添加 `/.codegraph/`，不修改任何已跟踪文件。CodeGraph 要求索引目录必须在项目根目录内，无法放到仓库外。

## 11. 离线部署

在一台**与目标机相同操作系统和 CPU 架构**、能联网的机器上准备：

```bash
./start.sh setup --pack-offline     # 安装并把 wheel 下载到 vendor/wheelhouse，生成 requirements.lock
```

然后把整个目录复制到内网机器，需要带上的内容：

| 内容 | 位置 | 说明 |
|---|---|---|
| Python 依赖 | `vendor/wheelhouse/`、`requirements.lock` | 与目标机的系统、架构、Python 小版本一致 |
| CodeGraph Node 包 | `vendor/node/`（已装好）或 `vendor/npm-cache/` | CodeGraph 含原生组件，必须同平台准备 |
| LLMWiki 源码与构建产物 | `vendor/src/llmwiki/` | 整体复制，必须含根 node_modules 与 packages/core/dist；offline 不 clone、不 npm ci、不构建 |
| ripgrep | `vendor/bin/rg.exe`（Windows）或 `vendor/bin/rg`（Linux） | 当前支持 Windows x64 MSVC / Linux x64 musl；offline 缺文件直接报错 |
| 模型 | `models/` | FastEmbed 的缓存目录，整体复制 |
| Node.js | 系统安装 | 20 以上、25 以下，建议 22 LTS |
| git | 系统安装 | 用于提交号和仓库新鲜度检查 |
| Python 3.11 | 系统安装 | 需支持加载 SQLite 扩展 |

内网机器上：

```bash
./start.sh setup --offline    # 只从 vendor/ 安装，不访问网络；缺什么会明确报出
./start.sh doctor
```

`kbw.yaml` 里设 `offline: true` 后，模型只从 `models/` 读取，找不到就降级，不会尝试下载。运行期任何情况下都不使用 `npx`。

Windows 准备的 `vendor/` 不能用于 Linux，需要在 Linux 机器上重新准备一次。

## 12. 测试

```bash
python -m pytest tests -q
```

`tests/fake_downstream.py` 和 `tests/stub_codegraph.py` 是使用真实 MCP 协议的替身进程，只用来测连接管理和路由逻辑，不能代替真实集成。真实集成用 `scripts/verify_real.py`。

## 13. 已知限制

- 第一版只有 stdio：每个 MCP 客户端各起一个网关进程，模型各加载一份。多人共用的 HTTP 常驻服务未实现。
- `codegraph_explore` 的返回是文本，网关按 1.6.2 的格式解析文件段和行号；上游改格式后需要调整 `kbw/code.py` 的 `parse_explore`。
- 只启用了 CodeGraph 默认的 `codegraph_explore` 一个工具，没有单独调用 callers / impact 等工具。
- 路由打分和"跨服务"判断是规则，不是模型；`depends_on` 和 `topics` 填得越准，跨服务扩查越准。
- 模型修订号尚未固定，首次 setup 后需要记录到 `upstream.lock.json`。

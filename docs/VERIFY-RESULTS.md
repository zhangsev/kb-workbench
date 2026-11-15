# Real integration verification

- date: 2026-11-15 00:29:15 +0800
- platform: Windows-11-10.0.26200-SP0
- python: 3.14.3
- config: D:\dev\project\kb-workbench\kbw.yaml

Timings are single measurements on this machine, not guarantees.

| step | status | ms |
|---|---|---|
| LLMWiki real MCP call (wiki_status, wiki_query, wiki_read_page) | PASS | 301 |
| LLMWiki write tools blocked by allow-list | PASS | 0 |
| CodeGraph real MCP call (codegraph_explore) | PASS | 1549 |
| CodeGraph second project through the same session (projectPath) | PASS | 1010 |
| Concurrent lazy start launches one process per downstream | PASS | 2473 |
| Recovery after downstream processes are killed | PASS | 5032 |
| Gateway end to end over MCP (knowledge -> page -> source, code, cross-service, cache) | PASS | 5438 |
| Clean shutdown leaves no child process | PASS | 2817 |

## LLMWiki real MCP call (wiki_status, wiki_query, wiki_read_page)

```json
{
  "tools": [
    "wiki_status",
    "wiki_query",
    "wiki_lint",
    "wiki_list_pages",
    "wiki_list_sources",
    "wiki_read_page",
    "wiki_read_index",
    "wiki_write_page",
    "wiki_create_entity",
    "wiki_create_concept",
    "wiki_update_page",
    "wiki_add_crosslinks",
    "wiki_update_index",
    "wiki_ingest_with_context",
    "wiki_fix",
    "wiki_delete_page"
  ],
  "cold_ms": 290.9,
  "warm_ms": 7.2,
  "status": {
    "command": "status",
    "api_version": "1",
    "source_count": 2,
    "wiki_page_count": 6,
    "last_ingest_date": null,
    "last_lint_date": null,
    "orphan_page_count": 0,
    "index_coverage_pct": 100
  },
  "first_result": {
    "title": "订单超时取消",
    "path": "concepts/order-timeout-cancel.md",
    "score": 12,
    "excerpt": "# 订单超时取消 ## 规则 订单创建后 30 分钟未支付会被自动取消，配置项为 `order.timeout-minutes`。 只有待支付（PENDING_PAYMENT）状态的订单可以被取消。 ## 流程 1. 定时任务调用 `OrderService.cancelOrder`。 2. 订单服务调用库存服务 `/api/inventory/release` 释放预占库存。 3. 订单服务发布…"
  },
  "read_page_chars": 522
}
```

## LLMWiki write tools blocked by allow-list

```json
"write tool refused by the gateway allow-list"
```

## CodeGraph real MCP call (codegraph_explore)

```json
{
  "tools": [
    "codegraph_explore"
  ],
  "cold_ms": 1536.1,
  "warm_ms": 12.9,
  "sections": [
    {
      "path": "src/main/java/com/example/order/OrderService.java",
      "lines": [
        1,
        39
      ]
    },
    {
      "path": "src/main/java/com/example/order/OrderController.java",
      "lines": [
        1,
        20
      ]
    },
    {
      "path": "src/main/java/com/example/order/Order.java",
      "lines": [
        1,
        22
      ]
    },
    {
      "path": "src/main/java/com/example/order/OrderStatus.java",
      "lines": [
        1,
        3
      ]
    }
  ],
  "raw_head": "**Exploration: OrderService cancelOrder**\n\nFound 24 symbols across 4 files.\n\n**Blast radius — what depends on these (update/verify before editing)**\n\n- `cancelOrder` (src/main/java/com/example/order/OrderService.java:30) — 1 caller in `src/main/java/com/example/order/OrderController.java`; no tests found within 3 caller hops\n- `OrderService` (src/main/java/com/example/order/OrderService.java:4) — 2 callers in `src/main/java/com/example/order/OrderController.java`; no tests found within 3 caller hops\n- `Order` (src/main/java/com/example/order/Order.java:8) — 1 caller in `src/main/java/com/examp"
}
```

## CodeGraph second project through the same session (projectPath)

```json
{
  "same_session_starts": 1,
  "paths": [
    "src/main/java/com/example/inventory/StockService.java",
    "src/main/java/com/example/inventory/InventoryController.java",
    "src/main/java/com/example/inventory/OrderCancelledListener.java"
  ]
}
```

## Concurrent lazy start launches one process per downstream

```json
{
  "llmwiki_starts": 1,
  "codegraph_starts": 1,
  "children": [
    {
      "pid": 12736,
      "name": "node.exe",
      "cmd": "\"C:\\Program Files\\nodejs\\node.EXE\" D:\\dev\\project\\kb-workbench\\vendor\\src\\llmwiki\\packages\\core\\dist\\mcp\\bin.js D:\\dev\\project\\kb-workbench\\examples\\kb\\.wiki"
    },
    {
      "pid": 47404,
      "name": "node.exe",
      "cmd": "\"C:\\Program Files\\nodejs\\node.EXE\" D:\\dev\\project\\kb-workbench\\vendor\\node\\node_modules\\@colbymchenry\\codegraph\\npm-shim.js serve --mcp"
    }
  ]
}
```

## Recovery after downstream processes are killed

```json
{
  "killed": [
    12736,
    47404
  ],
  "recovered_in_ms": 1682.9,
  "new_children": [
    52196,
    51260
  ]
}
```

## Gateway end to end over MCP (knowledge -> page -> source, code, cross-service, cache)

```json
{
  "tools": [
    "read_page",
    "read_source",
    "search_all",
    "search_code",
    "search_knowledge",
    "service_status"
  ],
  "search_knowledge_cold_ms": 694.2,
  "knowledge_routes": [
    "fulltext",
    "semantic"
  ],
  "knowledge_degraded": [],
  "search_knowledge_warm_ms": 17.4,
  "read_page_ms": 8.5,
  "read_page_served_by": "llmwiki.wiki_read_page",
  "read_source_ms": 2.3,
  "provenance_chain": "concepts/order-timeout-cancel.md -> sources/order-timeout-design-summary.md -> raw/order-timeout-design.md",
  "search_code_cold_ms": 2180.0,
  "code_top": {
    "repo": "order-service",
    "commit": "83df0f340b0a196e004b477d238e01a315bf3b7d",
    "path": "src/main/java/com/example/order/OrderService.java",
    "lines": [
      1,
      39
    ],
    "index_freshness": "fresh",
    "route_ranks": {
      "codegraph:order-service": 1,
      "ripgrep:order-service": 1
    }
  },
  "code_degraded": [],
  "search_code_warm_ms": 335.4,
  "search_code_cached_ms": 146.2,
  "cross_service_repos": [
    "order-service",
    "inventory-service"
  ],
  "search_all_ms": 465.3,
  "search_all_kinds": [
    "code",
    "knowledge"
  ],
  "downstream_states": {
    "llmwiki": "connected",
    "codegraph": "connected"
  },
  "models": {
    "embedding": {
      "model": "BAAI/bge-small-zh-v1.5",
      "state": "loaded",
      "reason": null
    },
    "reranker": {
      "model": "BAAI/bge-reranker-base",
      "state": "unavailable",
      "reason": "disabled in config"
    }
  }
}
```

## Clean shutdown leaves no child process

```json
"no child process left; CodeGraph's own shared daemon (if it started one) is intentionally not touched"
```

## Windows 验证小结

更新时间：2026-11-15（Asia/Shanghai），第二轮。**本轮要求的六组验证全部通过：setup/index/doctor 退出码均 0，pytest 18 项通过，真实 MCP 8/8 通过，后台 serve/status/stop 通过。** 没有修改、放宽或删除原有测试及 verify_real.py 的任何断言。

### 环境、安装位置和实际版本

- Windows 11 10.0.26200 x64；Python **3.14.3**（py -0 只有 3.14），Node **24.14.0**，npm **11.9.0**，Git **2.54.0.windows.1**。
- mcp **2.3.0**、fastembed **0.9.1**、sqlite-vec **0.1.9**、onnxruntime **1.32.0**、PyYAML **6.0.3**、pytest **9.2.0**，安装在项目 `.venv`。
- LLMWiki core **0.1.2**，现在是**源码构建**而非 npm 安装：`vendor/src/llmwiki`，提交 **32da49bf97743431a8cb3ff46e0e31c7fc629b18**。在源码根目录执行 `npm ci --cache <项目绝对路径>/vendor/npm-cache --no-audit --no-fund`，随后 `npm run build --workspace=packages/core --cache <同一缓存目录>`。根 README/package.json/core package.json 已核实，core 构建脚本为 tsc，未执行 VS Code 扩展构建。
- CodeGraph 主包及 `@colbymchenry/codegraph-win32-x64` 均为 **1.6.2**，位于 `vendor/node`。
- ripgrep **14.1.1**（rev 4649aa9700），安装于 `vendor/bin/rg.exe`，不修改系统 PATH。官方 Windows x64 MSVC ZIP 的 SHA-256 为 **d0f534024c42afd6cb4d38907c25cd2b249b79bbe6cc1dbee8e3e37c2b6e25a1**，下载官方 `.sha256` 后实际比对成功。下载和解压只使用 Python 标准库；只提取预期可执行文件，不展开其他压缩包路径。
- 嵌入模型仍为 BAAI/bge-small-zh-v1.5，FastEmbed 下载来源 `Qdrant/bge-small-zh-v1.5`，snapshot `46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59`；重排模型仍禁用。
- 所有安装通过项目 setup 执行，下载、缓存和临时文件限于项目目录；仍沿用进程级 HOME/USERPROFILE 指向 `.runtime-home`、TEMP/TMP 指向 `.tmp` 等隔离设置，没有全局安装、改系统配置或执行项目代码提交。setup 本轮未创建新的示例初始提交（仓库已存在）；原有测试创建临时示例时沿用项目自带的初始化行为。

### 本轮各步骤结果与证据

| 步骤 | 最终结果 | 说明/日志（docs/windows-logs 下） |
|---|---|---|
| setup --examples | **通过，退出码 0** | LLMWiki 固定提交源码安装并构建 core、ripgrep 下载校验、CodeGraph npm 安装、模型与示例检查均成功；round2-setup-final.txt |
| index | **通过，退出码 0** | 两仓库 CodeGraph sync 退出码均 0；知识 8 页、15 chunks，semantic=true、vectors=true；round2-index.txt |
| doctor | **通过，退出码 0** | **0 failed, 0 warnings**，使用本地 rg 和源码构建的 LLMWiki 入口；完整输出 round2-doctor.txt |
| python -m pytest tests -q | **通过，退出码 0** | **18 passed, 1 warning in 18.88s**：原 10 项、本轮之前 2 项兼容回归及本轮 6 项安装回归全部通过；round2-pytest.txt |
| scripts/verify_real.py | **通过，退出码 0，8/8** | 所有原断言通过；round2-verify.txt；完整原始报告 round2-verify-report.md |
| scripts/verify_windows_lifecycle.py | **通过，退出码 0** | stdin 管道保持打开，status 找到网关，stop 成功停止，随后 running_gateways=[]；round2-lifecycle.txt |
| 真实格式与离线局部检查 | **通过** | 实际中文查询和返回结构；已安装源码/ripgrep 的 offline 检查不执行任何 clone/npm/download；round2-formats-offline.txt。不是整套离线部署验收 |

两仓库在本轮生命周期 status 中均 `dirty=false`、`freshness=fresh`；源码构建没有修改示例业务文件。最终原样 `git status --porcelain` 检查见 round2-final-integrity.txt。

### LLMWiki 真实返回、中文搜索与网关兼容性

- 原脚本查询 **“订单 超时”成功，3 个结果**，第一项 `concepts/order-timeout-cancel.md`、score=12。
- `wiki_query.results[].path` 是**相对 wiki/ 的路径，不带 wiki/ 前缀**。相同路径直接传给 `wiki_read_page` 成功。现有 knowledge.py 的规范化逻辑与之兼容，无需修复。
- `wiki_query` 同时返回 content 中的 JSON 文本和 structured_content，两者相等；`wiki_read_page` 返回 content 中的 JSON 文本，**没有 structured_content**，解析对象含 `frontmatter` 和 `body`。现有 downstream.call() 正确提取文本 JSON，knowledge.read_page() 保留该页面对象；未触发本地文件降级。实际 served_by 为 `llmwiki.wiki_read_page`。
- 上游 query.ts 用空白拆分查询，先按索引标题/摘要的子串出现次数筛选，再加正文分数；它不是中文分词器。两个有空格的短词能命中本次示例；网关完整自然语言“订单超时取消的规则”的知识 routes_used 只有 fulltext、semantic，wiki_query 可正常响应但未贡献命中。这是正常的零结果，不是连接故障，未改查询/示例/断言来掩盖。
- 本轮没有修改 knowledge.py、downstream.py 或 parse_explore；真实协议已兼容。端到端完整验证了溯源链、两仓库跨服务查询、code/knowledge 融合和精确缓存命中。

### verify_real.py 八步骤耗时

以下均为本机单次测量，单位 ms；步骤总耗时与内部冷/热查询口径不同。

| 步骤 | 状态 | 总耗时 ms |
|---|---|---:|
| LLMWiki real MCP call | PASS | 301 |
| LLMWiki write tools blocked | PASS | 0 |
| CodeGraph real MCP call | PASS | 1549 |
| CodeGraph second project / same session | PASS | 1010 |
| Concurrent lazy start | PASS | 2473 |
| Recovery after downstream killed | PASS | 5032 |
| Gateway end to end over MCP | PASS | 5438 |
| Clean shutdown leaves no child process | PASS | 2817 |

| 原脚本记录的内部指标 | 耗时 ms |
|---|---:|
| LLMWiki 冷启动 wiki_status | **290.9** |
| LLMWiki 热查询 wiki_query | **7.2** |
| CodeGraph 冷启动首次 explore | **1536.1** |
| CodeGraph 热查询 explore | **12.9** |
| 网关 search_knowledge 冷查询 | **694.2** |
| 网关 search_knowledge 热查询（不同查询） | **17.4** |
| 网关 read_page / read_source | 8.5 / 2.3 |
| 网关 search_code 冷查询 | **2180.0** |
| 网关 search_code 热查询（不同查询） | **335.4** |
| 网关 search_code 相同查询缓存命中 | **146.2** |
| 网关 search_all | 465.3 |

缓存命中耗时包含版本/新鲜度检查等开销，不能当作纯数据库 cache_get 耗时；记录的“冷”指脚本首次调用，并非重启操作系统或清空文件系统缓存后的基准。崩溃恢复内部 recovered_in_ms=1682.9；关闭检查通过表示脚本检查的直接子进程为空，不扩大为系统全部进程的保证。

### 保留的上一轮修改记录（历史）

1. **start.ps1**：探测不存在的 py -3.11 时临时使用 Continue，随后恢复 Stop，避免 Windows PowerShell 将 native stderr 变成终止错误；将剩余参数显式保留为数组，防止单个 `--examples` 被展开成 `- - e x a m p l e s`。
2. **kbw/cli.py**：npm 包分别调用项目安装命令，某个包 E404 不阻断另一个包；移除不可用的 `--local-only`，向 Git 本地 info/exclude 幂等添加 `/.codegraph/`。真实上游生成的 .codegraph/.gitignore 会反向放行自身，仅靠它无法保持仓库干净；未修改已跟踪文件，也未隐藏其他源码修改。删除安装失败时建议另行全局安装的提示。
3. **kbw/config.py**：CodeGraph 入口改为真实发布的 `npm-shim.js`；默认参数移除真实 CLI 不支持的 `--no-telemetry`，继续用 DO_NOT_TRACK=1 / CODEGRAPH_TELEMETRY=0 关闭统计；增加 CODEGRAPH_NO_DOWNLOAD=1，阻止 shim 在运行期隐式下载到家目录。
4. **upstream.lock.json**：同步真实入口/参数，记录 LLMWiki E404、实际模型 snapshot 和重排模型未验证状态。包版本无需替换：CodeGraph 1.6.2 确实存在，LLMWiki 无法列出任何替代版本，所以保留请求版本而非捏造已安装版本。
5. **scripts/verify_real.py**：仅改失败日志，递归展开嵌套 ExceptionGroup 并保留原始 traceback，避免报告只显示 `unhandled errors in a TaskGroup` 或空 AssertionError；所有测试步骤和断言原样保留。
6. **tests/test_windows_compat.py**（新增）：两个回归测试，验证 Git 本地排除规则幂等且不改跟踪文件，以及运行期禁止隐式下载、关闭统计。两项通过。
7. **scripts/verify_windows_lifecycle.py**（新增）：可复跑的 Windows 生命周期验收，使用真实 start.ps1 status/stop、保留 serve stdin 管道、核验 PID 和停止后状态。
8. **README.md、kbw.example.yaml**：同步真实 CLI 参数和 Git 本地排除行为。
9. **.gitignore**：忽略本轮项目内 .cache、.tmp、.runtime-home，避免缓存混入源码交付。
10. **requirements.lock**（新增）：由项目 setup 自动生成的真实 Windows Python 3.14 环境依赖锁。**docs/VERIFY-RESULTS.md、docs/windows-logs/**：真实结果、原始日志及历史失败报告。

上一轮未改 code.py 等文件的表述仅适用于上一轮；本轮新增改动如下。上一轮失败结果和所有原始错误完整保存在 `windows-logs/round1-final-report.md`，并非被当作通过删除。

### 本轮修改记录

1. **kbw/config.py**：UPSTREAM 新增固定 LLMWiki commit 与 ripgrep 14.1.1；新增源码/构建产物路径常量，llmwiki_command 默认转向源码构建入口。
2. **kbw/install.py（新增）**：仅由 setup 调用的项目内安装实现。在线 clone（仅目录缺失时）、checkout 固定提交、npm ci、只构建 core；offline 只检查已复制的 node_modules/dist，缺失时明确提示复制目录，不运行 clone/npm。ripgrep 按当前平台选择 Windows x64 MSVC ZIP 或 Linux x64 musl tar.gz，下载官方 checksum 严格校验，只写 vendor/bin；已有文件不重复下载，offline 缺失时报告失败。
3. **kbw/cli.py**：不再请求 npm 的 @llmwiki/core；接入源码构建和本地 rg 安装，某一项失败不阻断其他项。doctor 改用统一 find_rg，源码入口检查与修复提示更新。
4. **kbw/util.py、kbw/code.py**：新增统一 find_rg，优先可执行的 vendor/bin/rg(.exe)，其次 PATH；代码检索使用该逻辑，不修改系统 PATH。
5. **start.ps1、start.sh**：按 vendor/bin 优先、PATH 其次的顺序检查 rg，缺失提示运行项目 setup。
6. **tests/test_install.py（新增）**：6 项回归，覆盖本地 rg 优先/PATH 回退、LLMWiki offline 不运行命令、固定提交及 core-only 构建、rg offline 不下载、官方文本格式 checksum/不匹配拒绝/限制提取路径、Linux x64 资产映射。没有改动任何原有断言。
7. **upstream.lock.json**：记录源码构建方式、固定 commit、实际 core 构建命令、本地入口、ripgrep 固定版本与平台资产/Windows 实测摘要。
8. **README.md、docs/M0-RESULTS.md**：不再指导从 npm 安装 LLMWiki；说明 core-only 构建、ripgrep 本地安装和离线必须携带 vendor/src/llmwiki（含 node_modules/dist）、vendor/bin。历史云端验证状态仍保留并注明不是最新结果。
9. **docs/VERIFY-RESULTS.md、docs/windows-logs/round2-* 文件**：更新真实结果、日志、协议格式探测与历史记录。

### 本轮遇到的失败、警告及仍未验证项

本轮首次 setup **失败**，原因不是下载错误，而是安装器最初假定 `.sha256` 首字段即摘要，实际官方 Windows 文件为：

```text
SHA256 hash of ripgrep-14.1.1-x86_64-pc-windows-msvc.zip:
d0f534024c42afd6cb4d38907c25cd2b249b79bbe6cc1dbee8e3e37c2b6e25a1
CertUtil: -hashfile command completed successfully.

ripgrep install failed: ValueError: ripgrep SHA256 mismatch:
expected sha256, got d0f534024c42afd6cb4d38907c25cd2b249b79bbe6cc1dbee8e3e37c2b6e25a1
```

已按实际格式修复为：要求校验文件**恰好包含一个 64 位十六进制摘要**，再严格比对文件摘要。未跳过校验，也未硬编码“视为通过”。首次失败原始日志保存在 round2-setup.txt；修复后的完整重跑成功，见 round2-setup-final.txt。

pytest 保留一个非断言警告：`PytestCacheWarning: could not create cache path ...\.pytest_cache\v\cache\nodeids: [WinError 183] 当文件已存在时，无法创建该文件。` 这是上一轮不同沙箱账号生成的缓存目录权限问题，不影响 18 项断言结果；没有通过关闭警告或修改测试掩盖。npm ci 对若干上游依赖发出 deprecated 警告，安装和 core 构建仍成功。PowerShell 对上游正常 stderr（如 Git checkout、CodeGraph 日志）包装出的 NativeCommandError 文本不代表非零进程退出码，最终实际退出码均已核对。

仍未验证：**整套离线安装/迁移、Linux 上实际安装和执行、Python 3.11、真实重排模型加载与 rerank**。本轮只验证了 Windows x64；Linux 资产已通过官方 release 清单核实，映射单元测试通过，但不能据此声称 Linux 运行通过。离线源码/rg 检查及“禁止联网”单元测试通过，不等同于 wheelhouse、Node 包、模型齐备后的全套离线部署验收。嵌入模型 revision 仍是实际下载记录，未强制固定后续 FastEmbed 下载 revision。

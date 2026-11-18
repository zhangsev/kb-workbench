# 知识沉淀：工具与 Agent 规则

kb-workbench 提供保存和整理知识的工具；**什么时候保存由调用方 Agent 按下面的规则判断**。网关不监听、不总结聊天内容，也不调用模型。工具装好以后，只有加载了这份规则的客户端和会话才会自动记录，不能保证所有客户端、所有会话都会记录。

## 1. 数据放在哪里

保存、检索、导航用的是同一套 LLMWiki 数据，不另建知识库：

| 内容 | 位置（都在 `kbw.yaml` 的 `knowledge.wiki_root` 下） |
|---|---|
| 业务实现、设计方案、排障结论 | `wiki/analyses/<主服务>/<slug>.md` |
| 通用框架或平台用法 | `wiki/frameworks/<slug>.md` |
| 按服务的导航页（自动生成） | `wiki/hubs/<服务>.md`、`wiki/hubs/frameworks.md` |
| 总索引、日志（LLMWiki 原有机制） | `wiki/index.md`、`wiki/log.md` |
| 稳定 ID、重定向、请求去重记录 | `wiki/.kbw/registry.json` |
| 原始材料（工具从不写入） | `raw/` |

页面是标准的 LLMWiki 页面（frontmatter 含 type、title、tags、created、updated），结构化内容放在 frontmatter 的 `kbw` 字段，正文由它生成。写入时使用与 LLMWiki 相同的写锁 `wiki/.llmwiki.lock`，两边不会交叉写坏文件。`search_knowledge` 搜的就是 `wiki/` 目录，保存后立即增量更新索引。

## 2. 工具

| 工具 | 用途 |
|---|---|
| `find_knowledge` | 保存前查找同主题页面：按标题、服务、关键词、引用文件、原问题打分，只给候选，不做决定 |
| `save_knowledge` | 新建或更新知识页（见下） |
| `read_page` | 按页面 ID 或路径读取；移动、合并过的页面用旧 ID、旧路径也能读到；返回结构化内容、版本号和 `source_check` |
| `search_knowledge` | 检索；`service` 参数只看某个服务；导航页不出现在结果中 |
| `check_knowledge` | 健康检查：重复候选、断链、孤立页、缺少来源、来源已变化、未进索引、待重建索引；`fix=true` 只做机械修复 |
| `organize_knowledge` | 移动或合并页面；不带 `plan_token` 只预览，带上才执行 |
| `reindex_knowledge` | 保存成功但索引失败时重试 |

终端里也可以执行：`python -m kbw check-knowledge [--fix]`，以及 `python -m kbw index --knowledge-only`（重建索引）。

### save_knowledge 的行为

- **新建**：给出 `slug`（英文小写和短横线）、`title`（稳定的主题名，不是提问原文）、`kind`、`summary`、`services`（第一个为主服务，决定目录）、`keywords`、`evidence`、`facts`。
- **更新**：给出 `page_id`。`mode=merge`（默认）只增加新内容，完全相同的表述（忽略标点、空白、大小写差异）自动合并，不删除任何内容。`mode=replace` 用整理后的完整内容重写，必须带 `expected_version`，没写进去的条目移到"历史结论"，不会直接消失。
- **同一 slug 再次新建**：同类型、同主服务、同 slug 视为同一主题，自动改为更新原页。
- **有相似页面时新建**：返回 `needs_decision` 和候选列表，不写入。同一主题就改用候选的 `page_id` 更新；确实是不同子主题，就带上 `distinct_from=[候选ID]` 再保存，两页会自动互相链接。
- **重复提交**：同一个 `request_id` 第二次返回 `duplicate_request`；内容完全没有变化时返回 `unchanged`，不重写文件。
- **并发与冲突**：所有写入都持有写锁，多个合并请求不会互相覆盖。`replace` 的版本号不一致时返回 `conflict`。页面正文被手工改过时返回 `conflict`，不会覆盖手工内容。
- **原子写入**：先写临时文件再整体替换，不会留下写了一半的页面。
- **校验**：路径必须是仓库内的相对路径；行号必须是 `[起, 止]`；提交号必须是 7 到 40 位十六进制；已确认事实必须引用至少一条源码依据；含密码、Token、Cookie、私钥等的内容整条拒绝。服务端只校验，不补造依据；没有给提交号时，会另外记录"保存时仓库的 HEAD"，并在页面上标明是保存时观察到的，不是分析依据。
- **索引**：返回 `index.status`。`indexed` 表示已经可以检索；`failed` 表示页面**已保存但暂时检索不到**，此时 `searchable` 为 false，用 `reindex_knowledge` 重试。

### 内容类型

| kind | 含义 | 页面上的标注 |
|---|---|---|
| `implementation` | 代码里现有的实现 | 已确认的现有实现 |
| `design` | 设计方案，尚未实现 | 设计方案（尚未实现，不代表现有行为） |
| `troubleshooting` | 排障结论 | 已确认的现有实现 + 排障信息 |
| `framework` | 通用框架或平台用法 | 已确认的用法；业务页通过 `related` 链接过来 |

一页之内同样区分：`facts`（已确认，必须有依据）、`designs`（待实施，未实现）、`inferences`（推断）、`open_questions`（待确认）、历史结论（被取代或已解答，由工具维护）。

## 3. Agent 规则（复制到 AGENTS.md 或 CLAUDE.md）

接入方式不变：客户端只需配置 kb-workbench 这一个 MCP（README 第 4 节）。把下面这段追加到 Agent 的长期指令文件：Codex 放在 `~/.codex/AGENTS.md`，Claude Code 放在 `~/.claude/CLAUDE.md`。如果只想对某个业务仓库生效，就放在该仓库根目录。

```
## 知识沉淀（kb-workbench）

何时保存：回答完以下几类问题、且结论有源码依据时，直接保存，不必询问用户——
业务链路或流程、框架/平台用法、业务规则、排障结论、已经确定的设计方案。
不保存：简单的语法或 API 问答、没有核实的猜测、原始搜索结果和工具输出、逐轮对话记录。
用户说"不要记录"时不保存。

保存步骤：
1. 先回答用户。保存在回答之后进行，保存失败不能影响回答本身。
2. 调用 find_knowledge，传入主题标题、服务、关键词、引用的文件（repo:path）和用户原问题。
   - 某个候选确实是同一主题：用它的 page_id 调用 save_knowledge（默认 merge）。
   - 是相关但不同的子主题：新建页面，并传 distinct_from=[候选 page_id]。
   - 相似度只是线索：同一主题 = 同一入口、同一流程、同一个问题或同一框架用法，不要仅凭分数判断。
3. 新建时：
   - title 用稳定的主题名（例如"告警推送规则的 CloudSOP 入库流程"），不要用用户的提问原文；
     原问题放进 question 字段。
   - slug 用英文小写短横线；services 第一个是主服务。
   - 通用框架用法（例如 CloudSOP 入库的通用写法）单独存一页，kind=framework；
     业务页用 related 链接过去，不要把通用用法重复写进每个业务页。
4. 内容要求：
   - facts 只写代码直接确认的内容，每条通过 evidence 引用 仓库、路径、类或方法、行号、
     提交号、分支；涉及未提交修改时设 dirty=true。
   - 推断写进 inferences，未确认的写进 open_questions。
   - 设计方案写进 designs，或整页 kind=design；不能写成已经实现的行为。
   - 不写密码、Token、Cookie 等凭据，不贴大段源码。
5. 更新已有页面：
   - 新发现直接 merge。
   - 已过时的结论用 supersede 列出它的条目 ID；已解答的问题用 resolve。
   - 需要整理重写时，先 read_page，再用 mode=replace 并带上 expected_version。
6. 涉及跨页合并、删除，或新旧结论冲突时：
   - 先用 organize_knowledge 预览，把差异和建议展示给用户，用户同意后再带 plan_token 执行。
   - 不自行删除页面。
7. 保存结果的报告：
   - 成功：在回答末尾用一行写明"已记录：<页面标题>（新建/更新）"。
   - index.status 为 failed：说明"页面已保存但暂未进入检索"，并调用 reindex_knowledge 重试。
   - 保存失败或返回 conflict/needs_decision 未能处理：如实说明原因。

引用已有知识时：
- 先用 search_knowledge 找到页面，再用 read_page 读取，看 source_check。
- source_check 为 unchanged：可以引用，同时说明依据的提交号。
- 为 needs_review 或 unknown：源码有变化或无法判断，这只是复核信号。必须先用 search_code /
  读取当前代码核对相关结论，不能直接当作仍然有效，也不能直接判定失效。核对后把结果 merge 回原页。
```

## 4. 整理

- **自动完成的**：页面归类目录、`index.md` 条目、服务导航页、明确指定的互相链接、移动后的链接修复。
- **只给建议、确认后才执行的**：跨页合并、页面移动（`organize_knowledge` 先预览）。
- **不提供的**：自动删除页面。确认某页整体作废时，由用户决定是否用 LLMWiki 的 `wiki_delete_page` 删除。
- 定期运行 `check_knowledge`，查看重复候选、断链、孤立页、缺少来源和来源已变化的页面。来源变化只是复核信号。

## 5. 边界

- kb-workbench 提供的是保存能力；是否保存由 Agent 规则触发。没加载规则的客户端或会话不会自动记录。
- 相似度只用于发现候选，不会据此自动合并或覆盖页面。
- 通过 LLMWiki 自己的写工具修改受管页面的正文，会被识别为"手工修改"，下次 `save_knowledge` 时返回冲突，需要先整理。

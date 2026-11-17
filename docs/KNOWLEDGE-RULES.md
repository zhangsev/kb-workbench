# 知识沉淀规则：分析完成后自动记录，同主题更新原页

适用于用 kb-workbench 做业务分析的 Agent（Codex、Claude Code）。把本文件第 3 节的规则复制到 Agent 的长期指令文件里（Codex：`~/.codex/AGENTS.md`；Claude Code：`~/.claude/CLAUDE.md`），并按第 2 节接好写入通道。

## 1. 能力从哪里来

| 动作 | 通过什么 | 说明 |
|---|---|---|
| 查重、检索 | kb-workbench 的 `search_knowledge`、`read_page` | 网关只读，写工具在网关侧被拒绝 |
| 新建页面 | LLMWiki MCP 的 `wiki_write_page` | 写 frontmatter（type、title、tags）和正文，自动登记到 `index.md` |
| 更新原页 | LLMWiki MCP 的 `wiki_update_page` | `bodyReplace` 替换正文，保留原有 frontmatter；改 title/tags 时同步 `index.md` |
| 列出页面 | LLMWiki MCP 的 `wiki_list_pages` | 判断路径是否已存在 |

写入后不需要任何额外操作：kb-workbench 每次 `search_knowledge` 前按文件修改时间增量更新索引，新内容立即可检索。`scripts/verify_wiki_write.py` 可在副本上验证整条链路。

上游工具的三个限制，规则里已经规避：

- `wiki_create_entity` / `wiki_create_concept` 用名称生成文件名，只保留英文字母和数字，**纯中文名称会被拒绝**。所以统一用 `wiki_write_page` 并显式给出英文路径。
- `wiki_write_page` 对已有页面是**整页覆盖**，frontmatter 只保留 type、title、tags，原来的 created、sources 等字段会丢失。所以已有页面一律用 `wiki_update_page`。
- 工具不能写自定义 frontmatter，也不会自动写 `updated`。所以仓库、分支、提交号、分析日期写在正文开头的"基线"表里。kb-workbench 的溯源信息会提示缺少 `updated`，属正常现象，以基线表为准。

## 2. 接入写入通道

在 Agent 的 MCP 配置里，除 kb-workbench 外再加一个 `llmwiki`，wiki 根目录必须和 `kbw.yaml` 的 `knowledge.wiki_root` 是同一个目录：

Codex（`~/.codex/config.toml`）：

```toml
[mcp_servers.llmwiki]
command = "node"
args = ['<kb-workbench 目录>\vendor\src\llmwiki\packages\core\dist\mcp\bin.js', '<wiki_root>']
```

Claude Code（`.mcp.json` 或用户级配置）：

```json
{ "mcpServers": { "llmwiki": {
  "command": "node",
  "args": ["<kb-workbench 目录>\\vendor\\src\\llmwiki\\packages\\core\\dist\\mcp\\bin.js", "<wiki_root>"]
} } }
```

## 3. 规则（复制到 Agent 的长期指令文件）

```
## 知识沉淀规则（kb-workbench / LLMWiki）

触发：每完成一次业务分析就执行，包括接手梳理、链路追踪、需求方案定稿、排障定位。
只做了检索、没有形成结论时不写。用户明确说"不要记录"时不写。

1. 确定主题和路径（路径只用英文小写和短横线，标题用中文）
   - 服务知识卡：entities/<仓库名>.md，一个仓库一页
   - 业务流程、规则、方案：concepts/<仓库名>-<主题英文短名>.md
   - 排障记录：concepts/<仓库名>-troubleshoot-<问题英文短名>.md
   - 跨服务链路：concepts/flow-<链路英文短名>.md

2. 查重，同主题必须更新原页，不新建
   - 先用 kb-workbench 的 search_knowledge 查：中文主题名 + 关键类名或接口路径
   - 再用 llmwiki 的 wiki_list_pages 确认目标路径是否已存在
   - 同主题 = 目标路径已存在；或已有页面基线中的仓库相同，且覆盖同一入口、同一流程或同一问题
   - 命中同主题页：用该页原路径，即使与第 1 条的命名不一致
   - 疑似相关但不同主题：新建页面，并在两页正文里互相加链接

3. 新建：调用 llmwiki 的 wiki_write_page（pagePath、title、type: entity 或 concept、tags、body）
   - 不使用 wiki_create_entity / wiki_create_concept（中文名称会被拒绝）

4. 更新原页：
   - 先用 read_page 读取当前全文
   - 合并：仍然成立的保留；被新代码证伪或已过时的修改或删除；新结论补进对应章节
   - 调用 llmwiki 的 wiki_update_page（pagePath、bodyReplace=合并后的完整正文）
   - 禁止对已有页面使用 wiki_write_page（会覆盖 frontmatter）；不要只用 bodyAppend 往末尾堆内容
   - 在"变更记录"末尾加一行

5. 正文模板
   # <中文标题>
   ## 基线
   | 仓库 | 分支 | 提交号 | 分析日期 | 分析范围 |
   ## 结论
   每条注明"代码确认"或"推断"，并附 仓库:路径:行号（类名.方法名）
   ## 待确认
   尚未确认的问题，以及需要什么证据确认
   ## 相关页面
   ## 变更记录
   - <日期> <提交号> 新建 / 更新了哪些部分

6. 不写入：把推断写成事实；密钥、口令、令牌、真实客户数据；大段源码（只写位置和要点）

7. 写完后用 search_knowledge 查一次，确认能召回新内容；
   在回复末尾用一行告诉用户：新建或更新了哪一页、路径、依据的提交号。
   写入失败时如实说明原因，不要静默跳过。
```

## 4. 不在本规则范围内

- 网关仍然保持只读，不增加写工具：检索链路不应有修改知识库的可能。
- 不自动删除页面。确认某页整体作废时，由用户决定是否调用 `wiki_delete_page`。

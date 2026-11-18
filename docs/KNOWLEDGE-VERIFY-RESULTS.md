# Knowledge saving: real verification

- date: 2026-11-18 23:38:56 +0800
- platform: Windows-11-10.0.26200-SP0
- python: 3.14.3
- evidence repo: order-service @ 83df0f340b0a

Runs on a temporary copy of the wiki. Timings are single measurements.

| step | status | ms |
|---|---|---|
| save framework page + business page linked to it (real MCP) | PASS | 1471 |
| search by question / service, read via LLMWiki wiki_read_page | PASS | 578 |
| three saves of one topic -> one integrated page; repeated request -> no new page | PASS | 647 |
| LLMWiki reads, indexes and lints the saved pages | PASS | 246 |
| gateway and LLMWiki write concurrently under the shared lock | PASS | 1341 |
| move page: old path and id still resolve, links fixed | PASS | 986 |
| existing knowledge search still works | PASS | 20 |
| raw material directory unchanged | PASS | 0 |

## save framework page + business page linked to it (real MCP)

```json
{
  "framework": {
    "page_id": "kb-434d157517",
    "path": "frameworks/kbw-verify-framework-usage.md",
    "status": "created",
    "index": {
      "status": "indexed",
      "pages_updated": 11,
      "pages_removed": 0,
      "semantic": true,
      "recovered": []
    },
    "_ms": 1162.1
  },
  "business": {
    "page_id": "kb-c1d424f08f",
    "path": "analyses/order-service/kbw-verify-business-flow.md",
    "status": "created",
    "index": {
      "status": "indexed",
      "pages_updated": 5,
      "pages_removed": 0,
      "semantic": true,
      "recovered": []
    },
    "_ms": 309.0
  }
}
```

## search by question / service, read via LLMWiki wiki_read_page

```json
{
  "search_ms": 412.8,
  "routes": [
    "fulltext",
    "semantic"
  ],
  "llmwiki_wiki_query_found_page": false,
  "read_ms": 144.2,
  "read_served_by": "llmwiki.wiki_read_page",
  "source_check": "unchanged",
  "degraded": []
}
```

## three saves of one topic -> one integrated page; repeated request -> no new page

```json
{
  "statuses": [
    "updated",
    "updated",
    "duplicate_request"
  ],
  "facts": [
    "校验：业务流程第一步",
    "校验：业务流程第二步",
    "校验：业务流程第三步"
  ],
  "questions": [
    "校验业务流程怎么走",
    "校验业务流程第二次",
    "校验业务流程第三次"
  ],
  "pages": [
    "analyses/order-service/kbw-verify-business-flow.md"
  ]
}
```

## LLMWiki reads, indexes and lints the saved pages

```json
{
  "wiki_query_found": true,
  "lint_issues_on_saved_pages": [],
  "lint_raw_head": "{\"command\":\"lint\",\"api_version\":\"1\",\"findings\":[{\"severity\":\"info\",\"category\":\"frontmatter-validation\",\"message\":\"Missing recommended \\\"tags\\\" field in \\\"sources/incident-2026-05-stock-leak-summary.md\\\"\",\"file\":\"sources/incident-2026-05-stock-leak-summary.md\"},{\"severity\":\"info\",\"category\":\"frontmatter-validation\",\"message\":\"Missing recommended \\\"created\\\" field in \\\"sources/incident-2026-05-stock"
}
```

## gateway and LLMWiki write concurrently under the shared lock

```json
{
  "llmwiki_target": "concepts/order-timeout-cancel.md",
  "gateway_saves": 5,
  "llmwiki_updates": 5,
  "pages_listed": 5741
}
```

## move page: old path and id still resolve, links fixed

```json
{
  "moved_to": "analyses/order-service/kbw-verify-business-flow-moved.md",
  "links_fixed": [
    "frameworks/kbw-verify-framework-usage.md",
    "hubs/order-service.md",
    "index.md"
  ],
  "check_summary": {
    "duplicates": 0,
    "broken_links": 0,
    "isolated": 3,
    "missing_sources": 1,
    "possibly_stale": 0,
    "not_in_index": 0,
    "pending_reindex": 0
  }
}
```

## existing knowledge search still works

```json
{
  "query": "订单超时取消",
  "top": "sources/order-timeout-design-summary.md"
}
```

## raw material directory unchanged

```json
null
```

"""Verify the knowledge write path with the real LLMWiki MCP server.

    python scripts/verify_wiki_write.py

Works on a temporary COPY of the configured wiki, so the real wiki is never
touched. Checks that a page can be created and then updated in place through
LLMWiki's own write tools, that the update keeps the page's frontmatter, and
that kb-workbench's search_knowledge finds the new content immediately.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kbw.config import downstream_env, load_config  # noqa: E402
from kbw.downstream import DownstreamToolError, ManagedSession  # noqa: E402
from kbw.gateway import Gateway  # noqa: E402
from kbw.knowledge import parse_page  # noqa: E402
from kbw.util import resolve_command  # noqa: E402

PAGE = "entities/kbw-write-check.md"
MARK1, MARK2 = "写入校验标记甲", "写入校验标记乙"
REQUIRED = ["wiki_write_page", "wiki_update_page", "wiki_read_page", "wiki_list_pages"]


async def main() -> int:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    if not cfg.wiki_root or not cfg.wiki_root.is_dir():
        print(f"knowledge.wiki_root not found: {cfg.wiki_root}")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="kbw-wiki-check-"))
    wiki = tmp / ".wiki"
    shutil.copytree(cfg.wiki_root, wiki)
    test_cfg = dataclasses.replace(cfg, wiki_root=wiki, data_dir=tmp / ".kbw", source_dirs=[wiki / "raw"])
    spec = resolve_command(*test_cfg.llmwiki_command())
    report: dict = {"temp_wiki": str(wiki)}
    session = ManagedSession("llmwiki-write", spec[0], spec[1], env=downstream_env(), cwd=str(tmp),
                             call_timeout=60, start_timeout=60, idle_timeout=0, required_tools=REQUIRED)
    gw = None
    try:
        await session.call("wiki_status")
        report["write_tools"] = [t for t in session.stats.tools if t not in
                                 ("wiki_status", "wiki_query", "wiki_lint", "wiki_list_pages",
                                  "wiki_list_sources", "wiki_read_page", "wiki_read_index")]

        # 1. create with a Chinese title and an explicit ASCII path
        body1 = f"# 写入校验服务\n\n## 基线\n\n| 仓库 | demo |\n|---|---|\n| 提交 | 0000000 |\n\n## 职责\n\n{MARK1}\n"
        r = await session.call("wiki_write_page", {"pagePath": PAGE, "title": "写入校验服务", "type": "entity",
                                                   "tags": ["服务知识卡", "kbw-check"], "body": body1})
        report["create"] = r["json"] or r["text"]
        fm1, _, _ = parse_page((wiki / "wiki" / PAGE).read_text(encoding="utf-8"))
        assert fm1.get("title") == "写入校验服务" and fm1.get("type") == "entity", fm1
        index = (wiki / "wiki" / "index.md").read_text(encoding="utf-8")
        assert PAGE in index, "page not registered in index.md"

        # 2. searchable through kb-workbench straight away (incremental index on query)
        gw = Gateway(test_cfg)
        hit = await gw.search_knowledge(MARK1)
        report["search_after_create"] = [x["page"] for x in hit["results"][:3]]
        assert hit["results"] and hit["results"][0]["page"] == PAGE, hit["results"][:2]

        # 3. update the SAME page in place: replace the body, keep frontmatter
        body2 = body1.replace(MARK1, MARK2) + "\n## 变更记录\n\n- 校验：更新原页\n"
        r = await session.call("wiki_update_page", {"pagePath": PAGE, "bodyReplace": body2})
        report["update"] = r["json"] or r["text"]
        text = (wiki / "wiki" / PAGE).read_text(encoding="utf-8")
        fm2, _, _ = parse_page(text)
        assert MARK2 in text and MARK1 not in text, "body was not replaced"
        assert fm2.get("title") == fm1.get("title") and fm2.get("tags") == fm1.get("tags"), (fm1, fm2)
        pages = list((wiki / "wiki").rglob("kbw-write-check*.md"))
        assert len(pages) == 1, f"expected one page, found {pages}"

        hit = await gw.search_knowledge(MARK2)
        report["search_after_update"] = [x["page"] for x in hit["results"][:3]]
        assert hit["results"] and hit["results"][0]["page"] == PAGE
        stale = await gw.search_knowledge(MARK1)
        assert not any(MARK1 in (x.get("snippet") or "") for x in stale["results"]), "old content still indexed"

        # 4. record what the helper tools do with Chinese names (informational, not a failure)
        try:
            r = await session.call("wiki_create_entity", {"name": "纯中文名称", "content": "x"})
            report["create_entity_chinese_name"] = r["json"] or r["text"]
        except DownstreamToolError as exc:
            report["create_entity_chinese_name"] = f"rejected: {exc}"
        report["frontmatter_after_update"] = fm2
        report["result"] = "PASS"
    except Exception as exc:  # report and fail
        report["result"] = f"FAILED: {type(exc).__name__}: {exc}"
    finally:
        if gw is not None:
            await gw.close()
        await session.close()
        shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

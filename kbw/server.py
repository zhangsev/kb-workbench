"""The gateway's MCP server (stdio). stdout carries only MCP protocol; logs go to stderr."""
from __future__ import annotations

import asyncio
from typing import Any
import atexit
import json
import os
import sys
import time
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import Config
from .gateway import Gateway
from .util import log

INSTRUCTIONS = """Retrieval over local knowledge pages and indexed source repositories, plus
saving of verified analysis results as reusable knowledge pages.
Typical flow: search_knowledge -> read_page -> read_source to verify against raw material;
search_code for symbols, call paths and source locations. Every answer lists `degraded`
reasons when a retrieval route was unavailable. Missing provenance is reported as missing.
After a finished analysis with source evidence: find_knowledge -> save_knowledge (update the
same-topic page, or create one). Source code is never modified; raw material is never written.
The server saves only what the caller submits; it does not summarise conversations."""


def build_server(cfg: Config) -> MCPServer:
    holder: dict[str, Gateway] = {}

    @asynccontextmanager
    async def lifespan(_server):
        holder["gw"] = Gateway(cfg)  # cheap: downstream sessions and models start lazily
        try:
            yield holder["gw"]
        finally:
            await holder["gw"].close()

    server = MCPServer("kb-workbench", instructions=INSTRUCTIONS, version=__version__, lifespan=lifespan)

    async def guarded(coro) -> str:
        try:
            result = await coro
        except (PermissionError, FileNotFoundError, ValueError, TimeoutError) as exc:
            result = {"error": type(exc).__name__, "message": str(exc)}
        return json.dumps(result, ensure_ascii=False, default=str)

    @server.tool()
    async def search_knowledge(query: str, top_k: int = 8, service: str | None = None) -> str:
        """Search knowledge pages (service cards, design notes, troubleshooting records, saved analyses).
        Fuses full-text, semantic (if the model is installed) and LLMWiki query rankings with RRF.
        `service` keeps only pages about that service. Returns page id, path, kind, snippet, sources
        and update time; missing provenance is flagged. Before relying on a saved analysis, read it
        with read_page and look at source_check."""
        return await guarded(holder["gw"].search_knowledge(query, top_k, service))

    @server.tool()
    async def read_page(path: str) -> str:
        """Read one knowledge page by page id (kb-...) or by path relative to the wiki pages
        directory. Ids and old paths of moved or merged pages still resolve (redirected_from).
        Saved analyses also return `knowledge` (structured content with item ids), `version`
        (pass as expected_version for a replace) and `source_check`: whether the cited source
        files changed since they were recorded. A change means re-check, not "wrong"."""
        return await guarded(holder["gw"].read_page(path))

    @server.tool()
    async def read_source(path: str, start_line: int = 1, max_lines: int = 400) -> str:
        """Read raw source material referenced by a page's provenance, to cross-check a page.
        Only files inside the configured source directories can be read."""
        return await guarded(asyncio.to_thread(holder["gw"].knowledge.read_source, path, start_line, max_lines))

    @server.tool()
    async def search_code(query: str, repos: list[str] | None = None, top_k: int = 8) -> str:
        """Search configured source repositories (indexed or not).
        Put identifiers in the query whenever you know them: class or method names, API paths,
        topics, config keys, file names, or an exact phrase from the code or its comments
        (Chinese phrases work). Exact text and file-name search covers every routed repository;
        call-graph results come only from indexed ones. If nothing in the query matches any
        repository, the result is empty with no_routing_signal=true and a hint, rather than
        unrelated code. When you already know the repository, pass repos=["name"]: only that
        repository is checked and searched. Chinese is also found in GBK-encoded files and in
        \\uXXXX-escaped .properties. A queried class name returns its defining file first with an
        `outline` of its declarations and method signatures. Returns repo, commit, relative
        path, line range with context, index freshness."""
        return await guarded(holder["gw"].search_code(query, repos, top_k))

    @server.tool()
    async def search_all(query: str, top_k: int = 8) -> str:
        """Search knowledge pages and source code together; rankings are merged with RRF."""
        return await guarded(holder["gw"].search_all(query, top_k))

    @server.tool()
    async def find_knowledge(title: str = "", services: list[str] | None = None,
                             keywords: list[str] | None = None, evidence_paths: list[str] | None = None,
                             question: str = "", kind: str | None = None, slug: str | None = None) -> str:
        """Find existing pages that may cover the same topic before saving. Give the stable
        topic title, services, keywords, cited files ("repo:path") and the user's question.
        Returns candidates with page_id, path, version, score and reasons. Scores only point
        at candidates: decide yourself whether a candidate is the same topic."""
        return await guarded(asyncio.to_thread(holder["gw"].notes.find, title, services, keywords,
                                               evidence_paths, question, slug, kind))

    @server.tool()
    async def save_knowledge(summary: str = "", title: str = "", kind: str | None = None,
                             services: list[str] | None = None, keywords: list[str] | None = None,
                             evidence: list[dict[str, Any]] | None = None,
                             facts: list[dict[str, Any]] | None = None,
                             designs: list[dict[str, Any]] | None = None,
                             inferences: list[str] | None = None, open_questions: list[str] | None = None,
                             question: str = "", page_id: str | None = None, slug: str | None = None,
                             mode: str = "merge", expected_version: str | None = None,
                             related: list[str] | None = None, distinct_from: list[str] | None = None,
                             supersede: list[str] | None = None, resolve: list[str] | None = None,
                             tags: list[str] | None = None, project: str | None = None,
                             request_id: str | None = None, change_note: str | None = None) -> str:
        """Save a verified analysis as a reusable knowledge page (create or update).

        Update: pass page_id (from find_knowledge/search_knowledge). mode=merge (default) adds new
        findings and collapses exact duplicates; nothing is dropped. mode=replace rewrites the page
        from your curated content and needs expected_version; items you leave out move to history.
        Create: pass slug (ascii, e.g. alarm-push-cloudsop-storage), title (a stable topic title,
        not the question), kind, summary, services (first = primary), keywords, evidence, facts.
        If a strong same-topic candidate exists the save returns needs_decision: use its page_id,
        or pass distinct_from=[ids] for a different sub-topic (pages get linked).

        kind: implementation | design | troubleshooting | framework (general usage; link business
        pages to it with related). evidence: [{ref, repo, path, symbol?, lines?: [start, end],
        commit?, branch?, dirty?}]. facts: confirmed statements, each with evidence refs:
        [{text, aspect: flow|rule|usage|fact, evidence: [ref]}]. designs: planned, NOT implemented.
        inferences / open_questions: unverified points. supersede: ids or texts of outdated items;
        resolve: answered open questions. question: the user's original question (kept for search).
        request_id: repeat-safe id for this save. Credentials are rejected.

        Returns status (created | updated | unchanged | duplicate_request | needs_decision |
        conflict), page_id, path, version, changes, warnings, and index.status: "indexed" means
        searchable now; "failed" means the page IS saved but not yet searchable (retry with
        reindex_knowledge)."""
        req = {k: v for k, v in dict(
            summary=summary, title=title, kind=kind, services=services, keywords=keywords, evidence=evidence,
            facts=facts, designs=designs, inferences=inferences, open_questions=open_questions,
            question=question, page_id=page_id, slug=slug, mode=mode, expected_version=expected_version,
            related=related, distinct_from=distinct_from, supersede=supersede, resolve=resolve, tags=tags,
            project=project, request_id=request_id, change_note=change_note).items() if v not in (None, "")}
        return await guarded(asyncio.to_thread(holder["gw"].notes.save, req))

    @server.tool()
    async def reindex_knowledge() -> str:
        """Retry the knowledge search-index refresh after save_knowledge reported index.status
        "failed". Pages are already saved; this only makes them searchable."""
        return await guarded(asyncio.to_thread(holder["gw"].notes.reindex))

    @server.tool()
    async def check_knowledge(fix: bool = False) -> str:
        """Health check of the knowledge base: duplicate candidates, broken links, isolated pages,
        missing sources, pages whose cited source files changed (a review signal only), pages
        missing from index.md, pending re-index. fix=true applies only mechanical repairs: links
        to moved pages, index entries and generated hub pages. It never merges or deletes."""
        return await guarded(asyncio.to_thread(holder["gw"].notes.check, fix))

    @server.tool()
    async def organize_knowledge(action: str, page: str, target: str | None = None,
                                 new_slug: str | None = None, plan_token: str | None = None) -> str:
        """Move (action=move, new_slug) or merge (action=merge, page into target) knowledge pages.
        Without plan_token it only previews: the plan, the content diff, possible conflicts and
        the pages whose links will be fixed, plus a plan_token. Show the preview to the user;
        apply by calling again with plan_token. Old paths and page ids keep resolving."""
        return await guarded(asyncio.to_thread(holder["gw"].notes.organize, action, page, target, new_slug,
                                               plan_token))

    @server.tool()
    async def service_status() -> str:
        """Health of the gateway: downstream connections, index state per repository,
        models, cache, and why any retrieval route is degraded."""
        return await guarded(holder["gw"].status())

    return server


def serve(cfg: Config) -> None:
    run_dir = cfg.data_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    pid_file = run_dir / f"{os.getpid()}.json"
    pid_file.write_text(json.dumps({"pid": os.getpid(), "started": time.time(), "config": str(cfg.file),
                                    "argv": sys.argv}), encoding="utf-8")
    atexit.register(lambda: pid_file.unlink(missing_ok=True))
    log.info("kb-workbench gateway %s serving on stdio (pid %s)", __version__, os.getpid())
    try:
        build_server(cfg).run("stdio")
    finally:
        pid_file.unlink(missing_ok=True)

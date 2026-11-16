"""The gateway's MCP server (stdio). stdout carries only MCP protocol; logs go to stderr."""
from __future__ import annotations

import asyncio
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

INSTRUCTIONS = """Read-only retrieval over local knowledge pages and indexed source repositories.
Typical flow: search_knowledge -> read_page -> read_source to verify against raw material;
search_code for symbols, call paths and source locations. Every answer lists `degraded`
reasons when a retrieval route was unavailable. Missing provenance is reported as missing.
This server never writes knowledge pages or source code."""


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
        except (PermissionError, FileNotFoundError, ValueError) as exc:
            result = {"error": type(exc).__name__, "message": str(exc)}
        return json.dumps(result, ensure_ascii=False, default=str)

    @server.tool()
    async def search_knowledge(query: str, top_k: int = 8) -> str:
        """Search knowledge pages (service cards, design notes, troubleshooting records).
        Fuses full-text, semantic (if the model is installed) and LLMWiki query rankings with RRF.
        Returns page path, snippet, line range, sources and update time; missing provenance is flagged."""
        return await guarded(holder["gw"].search_knowledge(query, top_k))

    @server.tool()
    async def read_page(path: str) -> str:
        """Read one knowledge page by its path relative to the wiki pages directory
        (as returned by search_knowledge), with its provenance."""
        return await guarded(holder["gw"].knowledge.read_page(path, asyncio.to_thread))

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

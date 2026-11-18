"""Knowledge retrieval over the LLMWiki pages directory.

Recall routes: FTS5 full text, sqlite-vec semantic (when the model is present),
and the downstream LLMWiki `wiki_query` tool. They are fused with RRF by page.
Provenance comes only from what the files actually contain; anything absent is
reported as missing, never invented.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .downstream import ManagedSession
from .embed import Embedder, Reranker
from .store import Store
from .util import log, rrf, safe_resolve, sha

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
MAX_CHUNK_CHARS = 1600


def parse_page(text: str) -> tuple[dict, list[str], int]:
    """Returns (frontmatter, body lines, 1-based line number of the first body line)."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                import yaml
                try:
                    fm = yaml.safe_load("\n".join(lines[1:i])) or {}
                except yaml.YAMLError:
                    fm = {}
                return (fm if isinstance(fm, dict) else {}), lines[i + 1:], i + 2
    return {}, lines, 1


def chunk_page(text: str, fallback_title: str) -> tuple[dict, list[dict]]:
    fm, body, first = parse_page(text)
    title = str(fm.get("title") or "")
    chunks, cur, heading, start = [], [], "", first

    def flush(end_line: int):
        content = "\n".join(cur).strip()
        if content:
            for off in range(0, len(content), MAX_CHUNK_CHARS):
                chunks.append({"heading": heading, "start_line": start, "end_line": end_line,
                               "text": content[off:off + MAX_CHUNK_CHARS]})

    for idx, line in enumerate(body):
        m = _HEADING.match(line)
        if m:
            flush(first + idx - 1)
            cur, heading, start = [line], m.group(2), first + idx
            if not title and len(m.group(1)) == 1:
                title = m.group(2)
        else:
            cur.append(line)
    flush(first + len(body) - 1)
    title = title or fallback_title
    for c in chunks:
        c["title"] = title
    return fm, chunks


class Knowledge:
    def __init__(self, cfg: Config, store: Store, embedder: Embedder, reranker: Reranker,
                 wiki: ManagedSession):
        self.cfg, self.store, self.embedder, self.reranker, self.wiki = cfg, store, embedder, reranker, wiki

    # ------------------------------------------------------------ index

    def _scan(self) -> dict[str, tuple[Path, str]]:
        root = self.cfg.wiki_pages_dir
        out = {}
        if root and root.is_dir():
            for p in sorted(root.rglob("*.md")):
                st = p.stat()
                out[p.relative_to(root).as_posix()] = (p, f"{st.st_mtime_ns}:{st.st_size}")
        return out

    def version(self) -> str:
        """Changes whenever any page is added, removed or modified."""
        return sha({k: v[1] for k, v in self._scan().items()})

    def sync(self, embed: bool = True) -> dict:
        """Incrementally bring the index in line with the files on disk."""
        t0 = time.perf_counter()
        files, known = self._scan(), self.store.file_sigs()
        use_vec = False
        if embed and self.store.vec_ok and self.embedder.load() is not None:
            probe = self.embedder.embed_passages(["维度探测"])
            self.store.ensure_vec_table(len(probe[0]), self.embedder.name)
            known = self.store.file_sigs()
            use_vec = True
        changed = [page for page, (_, sig) in files.items() if known.get(page) != sig]
        removed = [page for page in known if page not in files]
        for page in removed:
            self.store.delete_page(page)
        for page in changed:
            path, sig = files[page]
            fm, chunks = chunk_page(path.read_text(encoding="utf-8", errors="replace"), Path(page).stem)
            vectors = None
            if use_vec and chunks:
                vectors = self.embedder.embed_passages([f'{c["title"]} {c["heading"]}\n{c["text"]}' for c in chunks])
            self.store.add_page(page, sig, fm, chunks, vectors)
        if changed or removed:
            log.info("knowledge index: %d updated, %d removed", len(changed), len(removed))
        return {"updated": len(changed), "removed": len(removed), "semantic": use_vec,
                "ms": round((time.perf_counter() - t0) * 1000, 1), **self.store.counts()}

    # ------------------------------------------------------------ provenance

    def provenance(self, page: str) -> dict:
        fm = self.store.frontmatter(page) or {}
        root = self.cfg.wiki_pages_dir
        sources, missing = [], []
        k = fm.get("kbw") if isinstance(fm.get("kbw"), dict) else None
        if k is not None:  # page saved by save_knowledge: provenance is its source-code evidence
            for e in k.get("evidence") or []:
                sources.append({"evidence": e.get("id"), "repo": e.get("repo"), "path": e.get("path"),
                                "symbol": e.get("symbol"), "lines": e.get("lines"), "commit": e.get("commit"),
                                "branch": e.get("branch"), "dirty": e.get("dirty")})
            if not sources:
                missing.append("page has no source evidence")
            no_commit = [e.get("id") for e in k.get("evidence") or [] if not e.get("commit")]
            if no_commit:
                missing.append(f"evidence without commit: {no_commit}")
            updated = fm.get("updated")
            return {"page": page, "page_id": k.get("id"), "type": fm.get("type"), "kind": k.get("kind"),
                    "services": k.get("services") or [], "sources": sources,
                    "updated": str(updated) if updated else None, "missing": missing}
        listed = fm.get("sources") if isinstance(fm.get("sources"), list) else []
        own = fm.get("source_path")
        if own:
            sources.append({"via": page, "source_path": str(own)})
        for src_page in listed:
            src_fm = self.store.frontmatter(str(src_page))
            if src_fm is None:
                missing.append(f"source page not found: {src_page}")
            elif src_fm.get("source_path"):
                sources.append({"via": str(src_page), "source_path": str(src_fm["source_path"])})
            else:
                missing.append(f"source page has no source_path: {src_page}")
        if not listed and not own:
            missing.append("page declares no sources")
        mtime = None
        if root and (root / page).is_file():
            mtime = datetime.fromtimestamp((root / page).stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        updated = fm.get("updated")
        return {"page": page, "type": fm.get("type"), "sources": sources,
                "updated": str(updated) if updated else None, "file_modified": mtime,
                "missing": missing + ([] if updated else ["frontmatter has no 'updated' date"])}

    # ------------------------------------------------------------ search

    async def search(self, query: str, top_k: int, to_thread, service: str | None = None) -> dict:
        degraded, routes, timings = [], {}, {}
        await to_thread(self.sync)
        chunk_of: dict[str, dict] = {}

        def note(rows: list[dict]) -> list[str]:
            pages = []
            for r in rows:
                chunk_of.setdefault(r["page"], r)
                pages.append(r["page"])
            return pages

        t = time.perf_counter()
        routes["fulltext"] = note(await to_thread(self.store.search_fts, query))
        timings["fulltext_ms"] = round((time.perf_counter() - t) * 1000, 1)

        t = time.perf_counter()
        if not self.store.vec_ok:
            degraded.append(f"semantic: {self.store.vec_error}")
        elif not self.store.has_vectors():
            degraded.append(f"semantic: {self.embedder.status['reason'] or 'no vectors indexed yet; run `kbw index`'}")
        else:
            vec = await to_thread(self.embedder.embed_query, query)
            if vec is None:
                degraded.append(f"semantic: {self.embedder.status['reason']}")
            else:
                routes["semantic"] = note(await to_thread(self.store.search_vec, vec))
        timings["semantic_ms"] = round((time.perf_counter() - t) * 1000, 1)

        t = time.perf_counter()
        try:
            res = await self.wiki.call("wiki_query", {"query": query})
            data = res["json"] or res["structured"] or {}
            pages = []
            for item in data.get("results", []):
                page = str(item.get("path", "")).replace("\\", "/")
                page = page[5:] if page.startswith("wiki/") else page
                if page:
                    pages.append(page)
                    if page not in chunk_of:
                        chunk_of[page] = self.store.best_chunk(page) or {
                            "page": page, "title": item.get("title"), "heading": "", "start_line": None,
                            "end_line": None, "text": item.get("excerpt", "")}
            routes["wiki_query"] = pages
        except Exception as exc:
            degraded.append(f"wiki_query: {exc}")
        timings["wiki_query_ms"] = round((time.perf_counter() - t) * 1000, 1)

        # navigation pages (index, log, generated hubs) are not answers
        nav = lambda p: p in ("index.md", "log.md") or p.startswith("hubs/")
        fused = [x for x in rrf(routes, self.cfg.rrf_k) if not nav(x[0])]
        if service:
            want = service.casefold()

            def about(page: str) -> bool:
                fm = self.store.frontmatter(page) or {}
                k = fm.get("kbw") if isinstance(fm.get("kbw"), dict) else None
                if k is not None:
                    return want in {s.casefold() for s in k.get("services") or []}
                text = " ".join([str(fm.get("title") or ""), " ".join(map(str, fm.get("tags") or [])),
                                 (chunk_of.get(page) or {}).get("text") or ""]).casefold()
                return want in text
            fused = [x for x in fused if about(x[0])]
        pool = fused[:max(top_k * 3, top_k)]
        reranked = False
        if self.cfg.rerank_enabled and pool:
            scores = await to_thread(self.reranker.scores, query, [chunk_of[p]["text"] for p, _, _ in pool])
            if scores is None:
                degraded.append(f"rerank: {self.reranker.status['reason']}")
            else:
                pool = [x for _, x in sorted(zip(scores, pool), key=lambda z: -z[0])]
                reranked = True
        results = []
        for page, score, ranks in pool[:top_k]:
            c = chunk_of[page]
            prov = self.provenance(page)
            results.append({"page": page, "page_id": prov.get("page_id"), "kind": prov.get("kind"),
                            "services": prov.get("services"), "title": c.get("title"), "heading": c.get("heading"),
                            "lines": [c.get("start_line"), c.get("end_line")],
                            "snippet": (c.get("text") or "")[:500], "rrf_score": round(score, 5),
                            "route_ranks": ranks, "provenance": prov})
        return {"query": query, "service": service, "results": results, "fusion": f"RRF(k={self.cfg.rrf_k}) by page",
                "routes_used": [r for r, v in routes.items() if v], "reranked": reranked,
                "degraded": degraded, "timings": timings}

    # ------------------------------------------------------------ read

    async def read_page(self, page: str, to_thread) -> dict:
        root = self.cfg.wiki_pages_dir
        if not root:
            raise FileNotFoundError("knowledge.wiki_root is not configured")
        page = page.replace("\\", "/")
        page = page[5:] if page.startswith("wiki/") else page
        real = safe_resolve(page, [root])  # also blocks traversal for the local fallback
        await to_thread(self.sync, False)
        degraded, via = [], "llmwiki.wiki_read_page"
        try:
            res = await self.wiki.call("wiki_read_page", {"path": page})
            content = res["json"] if res["json"] is not None else res["text"]
        except Exception as exc:
            degraded.append(f"wiki_read_page: {exc}; served from the local file instead")
            via, content = "local_file", real.read_text(encoding="utf-8", errors="replace")
        return {"page": page, "served_by": via, "content": content,
                "provenance": self.provenance(page), "degraded": degraded}

    def read_source(self, path: str, start_line: int = 1, max_lines: int = 400) -> dict:
        if not self.cfg.source_dirs:
            raise PermissionError("no knowledge.source_dirs configured; read_source is disabled")
        try:
            real = safe_resolve(path, self.cfg.source_dirs)
        except FileNotFoundError:
            # LLMWiki records source_path relative to the wiki root (e.g. "raw/a.md").
            # The containment check against source_dirs still applies.
            if self.cfg.wiki_root is None or Path(path).is_absolute():
                raise
            real = safe_resolve(str(self.cfg.wiki_root / path), self.cfg.source_dirs)
        if not real.is_file():
            raise FileNotFoundError(f"not a file: {path}")
        if real.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("file larger than 20 MB; refused")
        data = real.read_bytes()
        if b"\x00" in data[:4096]:
            raise ValueError("binary file; refused")
        lines = data.decode("utf-8", errors="replace").splitlines()
        start = max(1, int(start_line))
        chunk = lines[start - 1:start - 1 + max(1, min(int(max_lines), 2000))]
        return {"path": str(real), "total_lines": len(lines), "start_line": start,
                "end_line": start + len(chunk) - 1, "truncated": start - 1 + len(chunk) < len(lines),
                "modified": datetime.fromtimestamp(real.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                "content": "\n".join(chunk)}

"""Code retrieval: routing over configured repos, CodeGraph queries, ripgrep exact
fallback, cross-service expansion, RRF fusion, provenance and index freshness."""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from pathlib import Path

from .config import Config, Repo
from .downstream import ManagedSession
from .store import Store
from .util import extract_symbols, find_rg, git_state, log, resolve_command, rrf, run

CROSS_HINTS = ("跨服务", "上下游", "上游", "下游", "调用链", "链路", "调用方", "谁调用", "哪些服务",
               "消费", "订阅", "发布", "cross-service", "upstream", "downstream", "caller")
_SECTION = re.compile(r"^\*\*`(.+?)`\*\*(?: — (.*))?$")
_NUMBERED = re.compile(r"^(\d+)\t")
RG_GLOBS = ["!.git", "!.codegraph*", "!target", "!build", "!node_modules", "!*.class", "!*.jar"]


def parse_explore(text: str) -> list[dict]:
    """Split codegraph_explore output into per-file sections.

    Upstream format (codegraph 1.6.2, src/mcp/tools.ts): each file section starts
    with a line "**`<path>`** — <symbols>", and source lines are "<n>\\t<code>".
    """
    sections, cur = [], None
    for line in text.splitlines():
        m = _SECTION.match(line)
        if m:
            cur = {"path": m.group(1), "symbols": m.group(2) or "", "body": [], "start": None, "end": None}
            sections.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("**"):  # trailing pointer list or another bold label
            cur = None
            continue
        n = _NUMBERED.match(line)
        if n:
            num = int(n.group(1))
            cur["start"] = num if cur["start"] is None else cur["start"]
            cur["end"] = num
        cur["body"].append(line)
    return sections


class Code:
    def __init__(self, cfg: Config, store: Store, codegraph: ManagedSession):
        self.cfg, self.store, self.codegraph = cfg, store, codegraph
        self._rg = find_rg()

    # ------------------------------------------------------------ state

    def repo_state(self, repo: Repo) -> dict:
        exists = repo.path.is_dir()
        indexed = exists and any(repo.path.glob(".codegraph*/codegraph.db"))
        state = git_state(repo.path) if exists else {"head": None, "dirty": None, "dirty_digest": None}
        rec = self.store.repo_index(repo.name) or {}
        if not exists:
            fresh = "repo_path_missing"
        elif not indexed:
            fresh = "not_indexed"
        elif not state["head"] or not rec.get("indexed_commit"):
            fresh = "unknown"
        elif rec["indexed_commit"] != state["head"]:
            fresh = "stale_head_moved"
        elif state["dirty"]:
            fresh = "uncommitted_changes"
        else:
            fresh = "fresh"
        return {"repo": repo.name, "path": str(repo.path), "exists": exists, "indexed": bool(indexed),
                "commit": state["head"], "dirty": state["dirty"], "dirty_digest": state["dirty_digest"],
                "indexed_commit": rec.get("indexed_commit"), "indexed_at": rec.get("indexed_at"),
                "freshness": fresh,
                "freshness_note": "kbw compares the commit recorded at `kbw index` with HEAD; "
                                  "CodeGraph's own watcher may already have synced newer changes"}

    def version(self, repos: list[Repo] | None = None) -> dict:
        out = {}
        for r in repos or self.cfg.repos:
            s = self.repo_state(r)
            out[r.name] = [s["commit"], s["dirty_digest"], s["indexed"], s["indexed_commit"]]
        return out

    # ------------------------------------------------------------ ripgrep

    def rg(self, repo: Repo, patterns: list[str], limit: int = 40) -> list[dict] | None:
        """Exact (fixed-string) search. None means ripgrep is not installed."""
        if not self._rg or not patterns or not repo.path.is_dir():
            return None if not self._rg else []
        cmd = [self._rg[0], *self._rg[1], "--json", "-F", "-n", "--max-count", "5", "--max-filesize", "2M"]
        for g in RG_GLOBS:
            cmd += ["-g", g]
        for p in patterns:
            cmd += ["-e", p]
        cmd.append(str(repo.path))
        try:
            proc = run(cmd, timeout=20)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
        hits = []
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") != "match":
                continue
            d = ev["data"]
            path = d["path"].get("text")
            if not path:
                continue
            text = (d["lines"].get("text") or "").rstrip("\n")
            hits.append({"path": Path(path).resolve().relative_to(repo.path.resolve()).as_posix(),
                         "line": d["line_number"], "text": text[:300],
                         "matched": [p for p in patterns if p in text]})
            if len(hits) >= limit:
                break
        return hits

    # ------------------------------------------------------------ routing

    def route(self, query: str, symbols: list[dict]) -> list[dict]:
        ql = query.lower()
        texts = [s["text"] for s in symbols]
        out = []
        for repo in self.cfg.repos:
            score, why = 0.0, []
            if repo.name.lower() in ql:
                score += 3; why.append("repo name")
            for pre in repo.package_prefixes:
                if pre.lower() in ql:
                    score += 3; why.append(f"package prefix {pre}")
            for kw in repo.keywords:
                if kw.lower() in ql:
                    score += 2; why.append(f"keyword {kw}")
            for topic in repo.topics:
                if topic.lower() in ql:
                    score += 2; why.append(f"topic {topic}")
            hits = self.rg(repo, texts, limit=200) if texts else []
            found = sorted({m for h in hits or [] for m in h["matched"]})
            if found:
                score += 2 * len(found); why.append(f"symbols present: {', '.join(found)}")
            out.append({"repo": repo.name, "score": score, "why": why, "symbols_found": found})
        out.sort(key=lambda r: -r["score"])
        return out

    def related(self, name: str) -> list[str]:
        repo = self.cfg.repo(name)
        if not repo:
            return []
        rel = [d for d in repo.depends_on if self.cfg.repo(d)]
        for other in self.cfg.repos:
            if other.name == name:
                continue
            if name in other.depends_on or set(other.topics) & set(repo.topics):
                rel.append(other.name)
        return list(dict.fromkeys(rel))

    # ------------------------------------------------------------ search

    async def _explore(self, repo: Repo, query: str) -> tuple[list[dict], str | None]:
        if not any(repo.path.glob(".codegraph*/codegraph.db")):
            return [], f"{repo.name}: no CodeGraph index (run `kbw index`)"
        try:
            res = await self.codegraph.call("codegraph_explore",
                                            {"query": query, "projectPath": str(repo.path)})
        except Exception as exc:
            return [], f"{repo.name}: codegraph: {exc}"
        return parse_explore(res["text"]), None

    async def search(self, query: str, repos: list[str] | None, top_k: int, to_thread) -> dict:
        t0 = time.perf_counter()
        symbols = extract_symbols(query)
        texts = [s["text"] for s in symbols]
        routing = await to_thread(self.route, query, symbols)
        if repos:
            unknown = [r for r in repos if not self.cfg.repo(r)]
            if unknown:
                raise ValueError(f"unknown repos: {unknown}; configured: {[r.name for r in self.cfg.repos]}")
            routing = [r for r in routing if r["repo"] in repos]
        degraded, queried, lists, items = [], [], {}, {}
        scored = [r for r in routing if r["score"] > 0]
        n = self.cfg.expand_top_n

        async def explore(names: list[str], reason: str) -> None:
            names = [x for x in names if x not in [q["repo"] for q in queried]]
            got = await asyncio.gather(*[self._explore(self.cfg.repo(x), query) for x in names])
            for name, (sections, err) in zip(names, got):
                queried.append({"repo": name, "reason": reason, "sections": len(sections)})
                if err:
                    degraded.append(err)
                keys = []
                for s in sections:
                    key = (name, s["path"])
                    keys.append(key)
                    items.setdefault(key, {}).update(
                        lines=[s["start"], s["end"]], symbols=s["symbols"],
                        snippet="\n".join(s["body"]).strip()[:1500])
                lists[f"codegraph:{name}"] = keys

        def sufficient() -> bool:
            sections = sum(q["sections"] for q in queried)
            blob = " ".join(v.get("snippet", "") + v.get("symbols", "") for v in items.values())
            return sections >= self.cfg.min_results and (not texts or any(t in blob for t in texts))

        if scored:
            await explore([scored[0]["repo"]], "top routed candidate")
        else:
            await explore([r["repo"] for r in routing[:n]], "no routing signal: top candidates in parallel")
        expanded = False
        if scored and not sufficient():
            expanded = True
            await explore([r["repo"] for r in routing[:n + 1]], "primary result insufficient: parallel expansion")
        cross = len(scored) >= 2 or any(h in query.lower() for h in CROSS_HINTS)
        if cross and queried:
            rel = [r["repo"] for r in scored[1:n + 1]]
            for q in list(queried)[:1]:
                rel += self.related(q["repo"])
            if repos:
                rel = [r for r in rel if r in repos]
            await explore(list(dict.fromkeys(rel))[:n + 1], "cross-service: related repos queried regardless of primary hits")

        # exact fallback over every repo touched (or routed), independent of CodeGraph
        rg_repos = list(dict.fromkeys([q["repo"] for q in queried] + [r["repo"] for r in scored]))
        if texts:
            if not self._rg:
                degraded.append("ripgrep not installed: exact fallback unavailable")
            for name in rg_repos:
                hits = await to_thread(self.rg, self.cfg.repo(name), texts)
                # rank files: a file named after a queried symbol first, then by how many
                # distinct queried strings it contains, then by number of matching lines
                per_file: dict[str, list] = {}
                for h in hits or []:
                    per_file.setdefault(h["path"], []).append(h)

                def weight(path: str):
                    hs = per_file[path]
                    return (-(Path(path).stem in texts), -len({m for h in hs for m in h["matched"]}), -len(hs), path)

                keys = []
                for h in (h for p in sorted(per_file, key=weight) for h in per_file[p]):
                    key = (name, h["path"])
                    keys.append(key)
                    it = items.setdefault(key, {})
                    it.setdefault("exact_hits", []).append({"line": h["line"], "text": h["text"]})
                    it.setdefault("lines", [h["line"], h["line"]])
                    it.setdefault("snippet", f'{h["line"]}\t{h["text"]}')
                lists[f"ripgrep:{name}"] = keys

        states = {name: await to_thread(self.repo_state, self.cfg.repo(name)) for name in rg_repos}
        results = []
        for (name, path), score, ranks in rrf(lists, self.cfg.rrf_k)[:top_k]:
            it, st = items[(name, path)], states.get(name) or self.repo_state(self.cfg.repo(name))
            results.append({"repo": name, "path": path, "lines": it.get("lines"), "commit": st["commit"],
                            "index_freshness": st["freshness"], "indexed_commit": st["indexed_commit"],
                            "symbols": it.get("symbols"), "snippet": it.get("snippet"),
                            "exact_hits": it.get("exact_hits", [])[:5],
                            "rrf_score": round(score, 5), "route_ranks": ranks})
        if results and not any(k.startswith("codegraph") and v for k, v in lists.items()):
            degraded.append("results come from exact text search only (no call graph or dependencies)")
        return {"query": query, "symbols": symbols, "routing": routing, "repos_queried": queried,
                "expanded": expanded, "cross_service": cross, "results": results,
                "fusion": f"RRF(k={self.cfg.rrf_k}) over per-repo CodeGraph and ripgrep rankings",
                "degraded": degraded, "repo_states": states,
                "timings": {"total_ms": round((time.perf_counter() - t0) * 1000, 1)}}

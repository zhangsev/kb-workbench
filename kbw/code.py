"""Code retrieval: routing over configured repos, CodeGraph queries, ripgrep exact
fallback, cross-service expansion, RRF fusion, provenance and index freshness."""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config, Repo
from .downstream import ManagedSession
from .store import Store
from .util import cjk_pieces, extract_terms, find_rg, git_state, log, resolve_command, rrf, run

CROSS_HINTS = ("跨服务", "上下游", "上游", "下游", "调用链", "链路", "调用方", "谁调用", "哪些服务",
               "消费", "订阅", "发布", "cross-service", "upstream", "downstream", "caller")
_SECTION = re.compile(r"^\*\*`(.+?)`\*\*(?: — (.*))?$")
_NUMBERED = re.compile(r"^(\d+)\t")
GIT_MEMO_SECONDS = 2.0
MAX_EXACT_REPOS = 8
CONTEXT_LINES = 3
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
        self._files: dict[str, tuple] = {}
        self._git_memo: dict[str, tuple] = {}

    # ------------------------------------------------------------ state

    def _git(self, repo: Repo, fresh: bool = False) -> dict:
        """git state, remembered for a moment: one search asks for it several
        times per repo, and each real check costs two git processes. The cache
        key (`version`) always re-reads it, so cached answers never go stale."""
        now = time.monotonic()
        memo = self._git_memo.get(repo.name)
        if not fresh and memo and now - memo[0] < GIT_MEMO_SECONDS:
            return memo[1]
        state = git_state(repo.path) if repo.path.is_dir() else {"head": None, "dirty": None, "dirty_digest": None}
        self._git_memo[repo.name] = (now, state)
        return state

    def repo_state(self, repo: Repo, fresh: bool = False) -> dict:
        exists = repo.path.is_dir()
        indexed = exists and any(repo.path.glob(".codegraph*/codegraph.db"))
        state = self._git(repo, fresh)
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
        repos = repos or self.cfg.repos
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(lambda r: self.repo_state(r, True), repos))
        return {r.name: [s["commit"], s["dirty_digest"], s["indexed"], s["indexed_commit"]]
                for r, s in zip(repos, states)}

    def coverage(self) -> dict:
        states = [self.repo_state(r) for r in self.cfg.repos]
        missing = [s["repo"] for s in states if not s["indexed"]]
        return {"configured": len(states), "indexed": len(states) - len(missing), "not_indexed": missing}

    # ------------------------------------------------------------ ripgrep

    def rg(self, repo: Repo, patterns: list[str], limit: int = 40, context: int = 0) -> list[dict] | None:
        """Exact, case-insensitive fixed-string search of file contents.
        None means ripgrep is not installed. With `context`, each hit carries the
        surrounding lines so the caller rarely needs to re-read the file."""
        if not self._rg or not patterns or not repo.path.is_dir():
            return None if not self._rg else []
        cmd = [self._rg[0], *self._rg[1], "--json", "-F", "-i", "-n", "--max-count", "5", "--max-filesize", "2M"]
        if context:
            cmd += ["-C", str(context)]
        for g in RG_GLOBS:
            cmd += ["-g", g]
        for p in patterns:
            cmd += ["-e", p]
        cmd.append(str(repo.path))
        try:
            proc = run(cmd, timeout=20)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
        root = repo.path.resolve()
        folded = [(p, p.casefold()) for p in patterns]
        hits, lines = [], {}
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") not in ("match", "context"):
                continue
            d = ev["data"]
            path = d["path"].get("text")
            if not path:
                continue
            try:
                rel = Path(path).resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            text = (d["lines"].get("text") or "").rstrip("\r\n")
            lines.setdefault(rel, {})[d["line_number"]] = text[:300]
            if ev["type"] == "match" and len(hits) < limit:
                low = text.casefold()
                hits.append({"path": rel, "line": d["line_number"], "text": text[:300],
                             "matched": [p for p, f in folded if f in low]})
        if context:
            for h in hits:
                near = lines[h["path"]]
                span = [n for n in range(h["line"] - context, h["line"] + context + 1) if n in near]
                h["span"] = [span[0], span[-1]]
                h["context"] = "\n".join(f"{n}\t{near[n]}" for n in span)
        return hits

    def files(self, repo: Repo) -> list[str]:
        """Relative paths of searchable files, cached until the repo's git state changes."""
        if not self._rg or not repo.path.is_dir():
            return []
        st = self._git(repo)
        key = (st["head"], st["dirty_digest"])
        cached = self._files.get(repo.name)
        if cached and cached[0] == key and key != (None, None):
            return cached[1]
        cmd = [self._rg[0], *self._rg[1], "--files"]
        for g in RG_GLOBS:
            cmd += ["-g", g]
        try:
            # run inside the repo so the output is already relative
            out = run(cmd, cwd=repo.path, timeout=30).stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
        paths = [p.replace("\\", "/") for p in out.splitlines() if p]
        self._files[repo.name] = (key, paths)
        return paths

    def path_hits(self, repo: Repo, patterns: list[str], limit: int = 40) -> list[dict]:
        """Files whose path or name contains a queried string (contents are not read)."""
        folded = [(p, p.casefold()) for p in patterns if "/" not in p.strip("/")]
        out = []
        for rel in self.files(repo):
            low = rel.casefold()
            matched = [p for p, f in folded if f in low]
            if matched:
                name = low.rsplit("/", 1)[-1]
                out.append({"path": rel, "matched": matched, "in_name": sum(f in name for _, f in folded)})
        out.sort(key=lambda h: (-h["in_name"], -len(h["matched"]), len(h["path"])))
        return out[:limit]

    # ------------------------------------------------------------ routing

    def route(self, query: str, terms: list[dict], only: list[str] | None = None) -> list[dict]:
        """Score every configured repo. Content and file-name checks use ripgrep,
        which needs no index, so unindexed repos are routed like indexed ones."""
        ql = query.lower()
        texts = [t["text"] for t in terms]
        weight = {t["text"]: 1 if t["kind"] == "cjk_piece" else 2 for t in terms}

        def one(repo: Repo) -> dict:
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
                score += sum(weight[m] for m in found); why.append(f"found in content: {', '.join(found)}")
            named = sorted({m for h in (self.path_hits(repo, texts) if texts else []) for m in h["matched"]})
            if named:
                score += sum(weight[m] for m in named); why.append(f"found in file names: {', '.join(named)}")
            return {"repo": repo.name, "score": score, "why": why, "terms_found": sorted(set(found) | set(named))}

        repos = [r for r in self.cfg.repos if not only or r.name in only]
        with ThreadPoolExecutor(max_workers=8) as pool:
            out = list(pool.map(one, repos))
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
        if repos:
            unknown = [r for r in repos if not self.cfg.repo(r)]
            if unknown:
                raise ValueError(f"unknown repos: {unknown}; configured: {[r.name for r in self.cfg.repos]}")
        terms = extract_terms(query)
        term_mode = "phrases"
        routing = await to_thread(self.route, query, terms, repos)
        found = {t.casefold() for r in routing for t in r["terms_found"]}
        missed = [t for t in terms if t["kind"] == "cjk" and t["text"].casefold() not in found]
        pieces = cjk_pieces(missed)
        if pieces:
            # A Chinese phrase that occurs nowhere usually carries extra words
            # ("子阵概览页面"): retry it as two-character pieces, at lower weight.
            terms = [t for t in terms if t not in missed] + pieces
            term_mode = "phrases+chinese_pieces" if found else "chinese_pieces"
            routing = await to_thread(self.route, query, terms, repos)
        texts = [t["text"] for t in terms]
        folded = [t.casefold() for t in texts]
        degraded, queried, lists, items = [], [], {}, {}
        scored = [r for r in routing if r["score"] > 0]
        n = self.cfg.expand_top_n
        base = {"query": query, "terms": terms, "term_mode": term_mode,
                "symbols": [t for t in terms if t["kind"] not in ("cjk", "cjk_piece", "word")],
                "fusion": f"RRF(k={self.cfg.rrf_k}) over per-repo CodeGraph, ripgrep and file-name rankings",
                "index_coverage": await to_thread(self.coverage)}

        if not scored and not repos:
            # Nothing in the question matches any repo's content, file names, name,
            # package prefix, keywords or topics. Guessing repos here would return
            # confident-looking but unrelated code, so return nothing and say why.
            return {**base, "routing": routing[:10], "repos_queried": [], "expanded": False,
                    "cross_service": False, "results": [], "no_routing_signal": True,
                    "hint": ("No configured repository matched this query. Put a class, method, API path, "
                             "topic, config key or an exact phrase from the code or its comments in the query; "
                             "or pass repos=[...] to search specific repositories; or add business keywords "
                             "to that repository in kbw.yaml."),
                    "degraded": [] if self._rg else ["ripgrep not installed: exact search unavailable"],
                    "repo_states": {}, "timings": {"total_ms": round((time.perf_counter() - t0) * 1000, 1)}}

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
            blob = " ".join(v.get("snippet", "") + v.get("symbols", "") for v in items.values()).casefold()
            return sections >= self.cfg.min_results and (not folded or any(t in blob for t in folded))

        if scored:
            await explore([scored[0]["repo"]], "top routed candidate")
        else:
            await explore([r["repo"] for r in routing[:n]], "no routing signal: repositories named by the caller")
        expanded = False
        if scored and not sufficient():
            expanded = True
            await explore([r["repo"] for r in scored[:n + 1]], "primary result insufficient: parallel expansion")
        cross = len(scored) >= 2 or any(h in query.lower() for h in CROSS_HINTS)
        if cross and queried:
            rel = [r["repo"] for r in scored[1:n + 1]]
            for q in list(queried)[:1]:
                rel += self.related(q["repo"])
            if repos:
                rel = [r for r in rel if r in repos]
            await explore(list(dict.fromkeys(rel))[:n + 1], "cross-service: related repos queried regardless of primary hits")

        # Exact search over every repo touched or routed. It does not depend on CodeGraph,
        # so it also covers repositories that have no index yet.
        rg_repos = list(dict.fromkeys([q["repo"] for q in queried] + [r["repo"] for r in scored]))[:MAX_EXACT_REPOS]
        if texts:
            if not self._rg:
                degraded.append("ripgrep not installed: exact search unavailable")
            for name in rg_repos:
                repo = self.cfg.repo(name)
                hits = await to_thread(self.rg, repo, texts, 40, CONTEXT_LINES)
                # rank files: one named after a queried term first, then by how many
                # distinct queried strings it contains, then by number of matching lines
                per_file: dict[str, list] = {}
                for h in hits or []:
                    per_file.setdefault(h["path"], []).append(h)

                def weight(path: str):
                    hs = per_file[path]
                    return (-(Path(path).stem.casefold() in folded), -len({m for h in hs for m in h["matched"]}),
                            -len(hs), path)

                keys = []
                for path in sorted(per_file, key=weight):
                    hs = per_file[path]
                    key = (name, path)
                    keys.append(key)
                    it = items.setdefault(key, {})
                    it["exact_hits"] = [{"line": h["line"], "text": h["text"]} for h in hs]
                    it["matched_terms"] = sorted({m for h in hs for m in h["matched"]})
                    it.setdefault("lines", [min(h["span"][0] for h in hs), max(h["span"][1] for h in hs)])
                    it.setdefault("snippet", "\n…\n".join(dict.fromkeys(h["context"] for h in hs[:3]))[:1500])
                lists[f"ripgrep:{name}"] = keys

                keys = []
                for h in await to_thread(self.path_hits, repo, texts):
                    key = (name, h["path"])
                    keys.append(key)
                    it = items.setdefault(key, {})
                    it["path_match"] = h["matched"]
                    if "snippet" not in it:
                        it["lines"], it["snippet"] = await to_thread(self._head, repo, h["path"])
                lists[f"filename:{name}"] = keys

        states = {name: await to_thread(self.repo_state, self.cfg.repo(name)) for name in rg_repos}
        results = []
        for (name, path), score, ranks in rrf(lists, self.cfg.rrf_k)[:top_k]:
            it, st = items[(name, path)], states.get(name) or self.repo_state(self.cfg.repo(name))
            results.append({"repo": name, "path": path, "lines": it.get("lines"), "commit": st["commit"],
                            "index_freshness": st["freshness"], "indexed_commit": st["indexed_commit"],
                            "symbols": it.get("symbols"), "matched_terms": it.get("matched_terms", []),
                            "path_match": it.get("path_match", []), "snippet": it.get("snippet"),
                            "exact_hits": it.get("exact_hits", [])[:5],
                            "rrf_score": round(score, 5), "route_ranks": ranks})
        if results and not any(k.startswith("codegraph") and v for k, v in lists.items()):
            degraded.append("results come from exact text search only (no call graph or dependencies)")
        out = {**base, "routing": routing[:10], "repos_queried": queried, "expanded": expanded,
               "cross_service": cross, "results": results, "no_routing_signal": not scored,
               "degraded": degraded, "repo_states": states,
               "timings": {"total_ms": round((time.perf_counter() - t0) * 1000, 1)}}
        if not results:
            out["hint"] = "No file matched. Try an exact identifier or phrase that appears in the code."
        return out

    @staticmethod
    def _head(repo: Repo, rel: str, lines: int = 25) -> tuple[list[int], str]:
        """First lines of a file matched by name only, so the caller sees what it is."""
        try:
            with open(repo.path / rel, encoding="utf-8", errors="replace") as fh:
                got = [fh.readline().rstrip("\r\n")[:300] for _ in range(lines)]
        except OSError:
            return [None, None], ""
        while got and not got[-1]:
            got.pop()
        return [1, len(got)], "\n".join(f"{i}\t{t}" for i, t in enumerate(got, 1))

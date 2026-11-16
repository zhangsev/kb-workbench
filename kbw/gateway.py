"""Wires config, store, models and downstream sessions; owns the exact cache."""
from __future__ import annotations

import asyncio
import time

from . import __version__
from .code import Code
from .config import LLMWIKI_READ_TOOLS, UPSTREAM, Config, downstream_env
from .downstream import ManagedSession
from .embed import Embedder, Reranker
from .knowledge import Knowledge
from .store import Store
from .util import resolve_command, rrf, sha


def _session(cfg: Config, name: str, spec, **kw) -> ManagedSession:
    common = dict(env=downstream_env(), call_timeout=cfg.call_timeout, start_timeout=cfg.start_timeout,
                  idle_timeout=cfg.idle_timeout, **kw)
    if spec is None:
        return ManagedSession(name, None, [], unavailable_reason="disabled or not configured", **common)
    resolved = resolve_command(*spec)
    if resolved is None:
        return ManagedSession(name, None, [], unavailable_reason=f"program not found: {spec[0]} (run `kbw doctor`)", **common)
    return ManagedSession(name, resolved[0], resolved[1], **common)


class Gateway:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.data_dir)
        self.embedder = Embedder(cfg.embedding_model, cfg.model_dir, cfg.offline)
        self.reranker = Reranker(cfg.reranker_model, cfg.model_dir, cfg.offline, enabled=cfg.rerank_enabled)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        # Read tools only: the gateway can never trigger a knowledge write.
        self.wiki = _session(cfg, "llmwiki", cfg.llmwiki_command(), cwd=str(cfg.data_dir),
                             required_tools=["wiki_query", "wiki_read_page"], allowed_tools=LLMWIKI_READ_TOOLS)
        # cwd has no index, so there is no default project: projectPath is always explicit.
        self.codegraph = _session(cfg, "codegraph", cfg.codegraph_command(), cwd=str(cfg.data_dir),
                                  required_tools=["codegraph_explore"], allowed_tools=["codegraph_explore"])
        self.knowledge = Knowledge(cfg, self.store, self.embedder, self.reranker, self.wiki)
        self.code = Code(cfg, self.store, self.codegraph)
        self.started = time.time()
        self.cache_hits = self.cache_misses = 0

    async def close(self) -> None:
        await asyncio.gather(self.wiki.close(), self.codegraph.close(), return_exceptions=True)
        self.store.close()

    async def _cached(self, tool: str, scope: dict, version, compute) -> dict:
        """Exact cache. The key covers the normalised query, the scope and the index
        version, so any change to sources, pages or indexes misses naturally.
        Degraded answers are not stored, so a recovered downstream is used at once."""
        key = sha({"tool": tool, "scope": scope, "version": version, "gateway": __version__})
        t0 = time.perf_counter()
        hit = await asyncio.to_thread(self.store.cache_get, key)
        if hit is not None:
            self.cache_hits += 1
            hit["cache"] = {"hit": True, "ms": round((time.perf_counter() - t0) * 1000, 1)}
            return hit
        self.cache_misses += 1
        value = await compute()
        value["cache"] = {"hit": False, "ms": round((time.perf_counter() - t0) * 1000, 1)}
        if not value.get("degraded"):
            await asyncio.to_thread(self.store.cache_put, key, value)
        return value

    def _knowledge_version(self) -> dict:
        return {"pages": self.knowledge.version(), "vectors": self.store.get_meta("vec"),
                "rerank": self.cfg.rerank_enabled}

    async def search_knowledge(self, query: str, top_k: int | None = None) -> dict:
        k = top_k or self.cfg.top_k
        version = await asyncio.to_thread(self._knowledge_version)
        return await self._cached("search_knowledge", {"q": " ".join(query.split()), "k": k}, version,
                                  lambda: self.knowledge.search(query, k, asyncio.to_thread))

    async def search_code(self, query: str, repos: list[str] | None = None, top_k: int | None = None) -> dict:
        k = top_k or self.cfg.top_k
        # Only the repos this query can touch decide whether a cached answer is still valid;
        # a single-repo query must not pay for git checks on every configured repo.
        scoped = [r for r in self.cfg.repos if r.name in repos] if repos else None
        version = await asyncio.to_thread(self.code.version, scoped)
        scope = {"q": " ".join(query.split()), "repos": sorted(repos or []), "k": k}
        return await self._cached("search_code", scope, version,
                                  lambda: self.code.search(query, repos, k, asyncio.to_thread))

    async def search_all(self, query: str, top_k: int | None = None) -> dict:
        k = top_k or self.cfg.top_k
        kn, cd = await asyncio.gather(self.search_knowledge(query, k), self.search_code(query, None, k),
                                      return_exceptions=True)
        degraded, lists, lookup = [], {}, {}
        for label, res in (("knowledge", kn), ("code", cd)):
            if isinstance(res, BaseException):
                degraded.append(f"{label}: {type(res).__name__}: {res}")
                continue
            degraded += [f"{label}: {d}" for d in res.get("degraded", [])]
            keys = []
            for r in res["results"]:
                key = (label, r.get("page") or f'{r["repo"]}:{r["path"]}')
                keys.append(key)
                lookup[key] = r
            lists[label] = keys
        merged = [{"kind": key[0], "rrf_score": round(score, 5), **lookup[key]}
                  for key, score, _ in rrf(lists, self.cfg.rrf_k)[:k * 2]]
        return {"query": query, "results": merged, "degraded": degraded,
                "fusion": "RRF over the knowledge ranking and the code ranking (ranks only, raw scores are not compared)",
                "knowledge": None if isinstance(kn, BaseException) else {k2: kn[k2] for k2 in ("routes_used", "timings", "cache")},
                "code": None if isinstance(cd, BaseException) else {k2: cd[k2] for k2 in ("routing", "repos_queried", "cross_service", "timings", "cache")}}

    async def status(self) -> dict:
        repos = [await asyncio.to_thread(self.code.repo_state, r) for r in self.cfg.repos]
        return {"gateway_version": __version__, "uptime_s": round(time.time() - self.started, 1),
                "config": str(self.cfg.file), "offline": self.cfg.offline, "upstream_pinned": UPSTREAM,
                "downstream": {"llmwiki": self.wiki.describe(), "codegraph": self.codegraph.describe()},
                "knowledge": {"wiki_root": str(self.cfg.wiki_root) if self.cfg.wiki_root else None,
                              "source_dirs": [str(d) for d in self.cfg.source_dirs], **self.store.counts(),
                              "sqlite_vec": {"loaded": self.store.vec_ok, "reason": self.store.vec_error}},
                "models": {"embedding": self.embedder.status, "reranker": self.reranker.status},
                "repos": repos,
                "index_coverage": {"configured": len(repos), "indexed": sum(r["indexed"] for r in repos),
                                   "not_indexed": [r["repo"] for r in repos if not r["indexed"]]},
                "cache": {"entries": self.store.cache_size(), "hits": self.cache_hits, "misses": self.cache_misses},
                "ripgrep": bool(self.code._rg)}

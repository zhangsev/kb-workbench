from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = {"llmwiki_core": "0.1.2", "llmwiki_commit": "32da49bf97743431a8cb3ff46e0e31c7fc629b18",
            "codegraph": "1.6.2", "ripgrep": "14.1.1"}
LLMWIKI_SOURCE = PROJECT_ROOT / "vendor" / "src" / "llmwiki"
LLMWIKI_ENTRY = LLMWIKI_SOURCE / "packages" / "core" / "dist" / "mcp" / "bin.js"
LLMWIKI_READ_TOOLS = ["wiki_status", "wiki_query", "wiki_list_pages", "wiki_list_sources",
                      "wiki_read_page", "wiki_read_index"]


@dataclass
class Repo:
    name: str
    path: Path
    package_prefixes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)


@dataclass
class Config:
    file: Path
    data_dir: Path
    offline: bool
    wiki_root: Path | None
    source_dirs: list[Path]
    model_dir: Path
    embedding_model: str
    reranker_model: str
    rerank_enabled: bool
    repos: list[Repo]
    min_results: int
    expand_top_n: int
    top_k: int
    rrf_k: int
    call_timeout: float
    start_timeout: float
    idle_timeout: float
    llmwiki: dict
    codegraph: dict

    @property
    def wiki_pages_dir(self) -> Path | None:
        return self.wiki_root / "wiki" if self.wiki_root else None

    @property
    def node_dir(self) -> Path:
        return PROJECT_ROOT / "vendor" / "node"

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)

    def llmwiki_command(self) -> tuple[str, list[str]] | None:
        """How to launch llmwiki-mcp. Never uses npx at runtime (no implicit download)."""
        if not self.llmwiki.get("enabled", True) or not self.wiki_root:
            return None
        if self.llmwiki.get("command"):
            return self.llmwiki["command"], [str(a) for a in self.llmwiki.get("args", [])] + [str(self.wiki_root)]
        return "node", [str(LLMWIKI_ENTRY), str(self.wiki_root)]

    def codegraph_command(self) -> tuple[str, list[str]] | None:
        if not self.codegraph.get("enabled", True):
            return None
        args = [str(a) for a in self.codegraph.get("args", ["serve", "--mcp"])]
        if self.codegraph.get("command"):
            return self.codegraph["command"], args
        local = self.node_dir / "node_modules" / "@colbymchenry" / "codegraph" / "npm-shim.js"
        if local.exists():
            return "node", [str(local), *args]
        return "codegraph", args

    def codegraph_cli(self) -> tuple[str, list[str]]:
        cmd, args = self.codegraph_command() or ("codegraph", [])
        return (cmd, [args[0]]) if cmd == "node" else (cmd, [])


def downstream_env() -> dict:
    """Environment for upstream programs: no telemetry, no update check."""
    env = dict(os.environ)
    env.update({"DO_NOT_TRACK": "1", "CODEGRAPH_TELEMETRY": "0", "CODEGRAPH_NO_UPDATE_CHECK": "1",
                "CODEGRAPH_NO_DOWNLOAD": "1"})
    return env


def find_config(explicit: str | None = None) -> Path:
    for cand in (explicit, os.environ.get("KBW_CONFIG"), "kbw.yaml", str(PROJECT_ROOT / "kbw.yaml")):
        if cand and Path(cand).is_file():
            return Path(cand).resolve()
    raise FileNotFoundError(
        "config not found. Copy kbw.example.yaml to kbw.yaml and edit it, "
        "or pass --config / set KBW_CONFIG.")


def load_config(explicit: str | None = None) -> Config:
    import yaml

    file = find_config(explicit)
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    base = file.parent

    def p(v) -> Path:
        q = Path(os.path.expandvars(os.path.expanduser(str(v))))
        return q if q.is_absolute() else (base / q).resolve()

    kn = raw.get("knowledge") or {}
    models = raw.get("models") or {}
    search = raw.get("search") or {}
    timeouts = raw.get("timeouts") or {}
    down = raw.get("downstream") or {}
    repos = []
    for r in raw.get("repos") or []:
        repos.append(Repo(name=str(r["name"]), path=p(r["path"]),
                          package_prefixes=list(r.get("package_prefixes") or []),
                          keywords=list(r.get("keywords") or []),
                          depends_on=list(r.get("depends_on") or []),
                          topics=list(r.get("topics") or [])))
    names = [r.name for r in repos]
    if len(set(names)) != len(names):
        raise ValueError("duplicate repo names in config")
    offline = bool(raw.get("offline", False)) or os.environ.get("KBW_OFFLINE") == "1"
    return Config(
        file=file, data_dir=p(raw.get("data_dir", ".kbw")), offline=offline,
        wiki_root=p(kn["wiki_root"]) if kn.get("wiki_root") else None,
        source_dirs=[p(d) for d in kn.get("source_dirs") or []],
        model_dir=p(models.get("dir", "models")),
        embedding_model=models.get("embedding", "BAAI/bge-small-zh-v1.5"),
        reranker_model=models.get("reranker", "BAAI/bge-reranker-base"),
        rerank_enabled=bool(models.get("rerank_enabled", False)),
        repos=repos,
        min_results=int(search.get("min_results", 3)),
        expand_top_n=int(search.get("expand_top_n", 3)),
        top_k=int(search.get("top_k", 8)), rrf_k=int(search.get("rrf_k", 60)),
        call_timeout=float(timeouts.get("call_seconds", 30)),
        start_timeout=float(timeouts.get("start_seconds", 30)),
        idle_timeout=float(timeouts.get("idle_seconds", 300)),
        llmwiki=down.get("llmwiki") or {}, codegraph=down.get("codegraph") or {},
    )


def venv_python() -> str:
    return sys.executable

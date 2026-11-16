"""Shared helpers. Everything here logs to stderr only: stdout belongs to MCP."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("kbw")


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s kbw %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def sha(obj) -> str:
    data = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def run(cmd: list[str], cwd: str | Path | None = None, timeout: float = 30,
        env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command, never through a shell. Raises FileNotFoundError if missing."""
    return subprocess.run(cmd, cwd=cwd, timeout=timeout, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL)


def resolve_command(command: str, args: list[str]) -> tuple[str, list[str]] | None:
    """Resolve an executable to an absolute path.

    Windows .cmd/.bat shims cannot be spawned directly without a shell, so they
    are wrapped in `cmd /c`. Returns None when the program is not installed.
    """
    path = command if os.path.isabs(command) and os.path.exists(command) else shutil.which(command)
    if not path:
        return None
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        return os.environ.get("COMSPEC", "cmd.exe"), ["/c", path, *args]
    return path, list(args)


def find_rg() -> tuple[str, list[str]] | None:
    """Prefer the project-local executable without modifying PATH."""
    from .config import PROJECT_ROOT
    local = PROJECT_ROOT / "vendor" / "bin" / ("rg.exe" if os.name == "nt" else "rg")
    if local.is_file() and os.access(local, os.X_OK):
        return str(local), []
    return resolve_command("rg", [])


# ---------------------------------------------------------------- git

def git(repo: Path, *args: str, timeout: float = 15) -> str | None:
    try:
        r = run(["git", "-C", str(repo), *args], timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def git_state(repo: Path) -> dict:
    """HEAD commit plus a digest of uncommitted changes (None values = unknown)."""
    head = git(repo, "rev-parse", "HEAD")
    porcelain = git(repo, "status", "--porcelain")
    dirty_digest = None
    if porcelain:
        parts = []
        for line in porcelain.splitlines():
            rel = line[3:].split(" -> ")[-1].strip().strip('"')
            p = repo / rel
            try:
                st = p.stat()
                parts.append((line[:2], rel, st.st_mtime_ns, st.st_size))
            except OSError:
                parts.append((line[:2], rel, 0, 0))
        dirty_digest = sha(parts)
    return {"head": head, "dirty": bool(porcelain) if porcelain is not None else None,
            "dirty_digest": dirty_digest}


# ---------------------------------------------------------------- text

_CJK = r"㐀-䶿一-鿿"
_WORD = re.compile(rf"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[{_CJK}]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def fts_tokens(text: str) -> list[str]:
    """Tokens for the FTS5 index and for queries (must be applied to both).

    Latin identifiers are kept whole and also split on camelCase/snake_case;
    CJK runs become overlapping bigrams so that two-character words match.
    """
    out: list[str] = []
    for m in _WORD.finditer(text):
        w = m.group(0)
        if re.match(rf"[{_CJK}]", w):
            out.extend([w] if len(w) == 1 else [w[i:i + 2] for i in range(len(w) - 1)])
            continue
        low = w.lower()
        out.append(low)
        parts = [p.lower() for seg in w.split("_") for p in _CAMEL.findall(seg)]
        if len(parts) > 1:
            out.extend(parts)
    return out


def fts_query(text: str) -> str:
    toks = list(dict.fromkeys(fts_tokens(text)))
    return " OR ".join(f'"{t}"' for t in toks)


_SYMBOL_PATTERNS = [
    ("path", re.compile(r"(?<![\w.])/[A-Za-z0-9_\-{}]+(?:/[A-Za-z0-9_\-{}.]+)+")),
    ("dotted", re.compile(r"\b[a-z][a-z0-9_\-]*(?:\.[A-Za-z][A-Za-z0-9_\-]*){1,}\b")),
    ("class", re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+\b")),
    ("method", re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+)+\b")),
    ("const", re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")),
    ("kebab", re.compile(r"\b[a-z0-9]+(?:-[a-z0-9]+)+\b")),
]


def extract_symbols(query: str) -> list[dict]:
    """Code-looking tokens in a question: class names, methods, API paths,
    dotted names (packages / topics / config keys), constants."""
    seen, out = set(), []
    for kind, pat in _SYMBOL_PATTERNS:
        for m in pat.finditer(query):
            s = m.group(0)
            if s not in seen and len(s) >= 3:
                seen.add(s)
                out.append({"text": s, "kind": kind})
    return out


_CJK_RUN = re.compile(rf"[{_CJK}]+")
# Only unambiguous particles and question/filler words split a Chinese phrase.
# Single-character prepositions are left alone because they also occur inside words.
_CJK_SPLIT = re.compile(r"[的了吗呢吧啊]|在哪里|在哪儿|在哪个|在哪些|在哪|为什么|怎么样|怎么|如何|哪里|哪儿|哪个|哪些|什么|是否|请问|帮我|帮忙|一下|有没有")
_CJK_STOP = {"实现", "代码", "逻辑", "功能", "接口", "方法", "服务", "流程", "位置", "地方", "相关", "处理",
             "调用", "查询", "查找", "配置", "问题", "这个", "那个", "我们", "项目", "文件", "模块", "需要", "可以"}
_WORD_RUN = re.compile(r"[A-Za-z][A-Za-z0-9]{3,}")
_WORD_STOP = {"what", "where", "which", "when", "does", "this", "that", "with", "from", "into", "have", "find",
              "show", "code", "file", "files", "class", "method", "function", "service", "interface", "impl",
              "about", "there", "their", "should", "would", "could", "used", "using", "call", "calls", "called",
              "implemented", "implementation", "logic", "handle", "handles", "return", "returns"}


def extract_terms(query: str) -> list[dict]:
    """Everything in a question worth an exact (fixed-string) search.

    Code symbols first (see extract_symbols), then Chinese phrases and plain
    words, so that a question written in natural language or copied from a
    Chinese comment still reaches ripgrep instead of being dropped.
    """
    out = extract_symbols(query)
    seen = {t["text"].casefold() for t in out}
    covered = " ".join(t["text"] for t in out).casefold()
    for run_ in _CJK_RUN.findall(query):
        for seg in _CJK_SPLIT.split(run_):
            if len(seg) >= 2 and seg not in _CJK_STOP and seg not in seen:
                seen.add(seg)
                out.append({"text": seg, "kind": "cjk"})
    for w in _WORD_RUN.findall(query):
        low = w.casefold()
        if low in seen or low in _WORD_STOP or low in covered:
            continue
        seen.add(low)
        out.append({"text": w, "kind": "word"})
    return out


def cjk_pieces(terms: list[dict]) -> list[dict]:
    """Second-chance terms: two-character pieces of Chinese phrases, used only
    when no whole phrase matched anywhere (e.g. the phrase carries extra words)."""
    seen, out = set(), []
    for t in terms:
        if t["kind"] != "cjk" or len(t["text"]) < 3:
            continue
        for i in range(len(t["text"]) - 1):
            piece = t["text"][i:i + 2]
            if piece not in seen and piece not in _CJK_STOP:
                seen.add(piece)
                out.append({"text": piece, "kind": "cjk_piece"})
    return out


def rrf(ranked_lists: dict[str, list], k: int = 60) -> list[tuple[object, float, dict]]:
    """Reciprocal Rank Fusion. Raw scores of different sources are never compared;
    only ranks are used. Returns [(key, score, {source: rank})] best first."""
    scores: dict[object, float] = {}
    ranks: dict[object, dict] = {}
    for source, keys in ranked_lists.items():
        seen = set()
        rank = 0
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            ranks.setdefault(key, {})[source] = rank
    return sorted(((key, s, ranks[key]) for key, s in scores.items()), key=lambda x: -x[1])


def safe_resolve(path: str, allowed_roots: list[Path]) -> Path:
    """Resolve `path` and require the real location to be inside an allowed root.

    Symlinks are followed before the check, so a link pointing outside the
    allowed directories is rejected. Raises PermissionError.
    """
    if "\x00" in path:
        raise PermissionError("invalid path")
    roots = [r.resolve() for r in allowed_roots]
    p = Path(path)
    candidates = [p] if p.is_absolute() else [r / p for r in roots]
    for cand in candidates:
        try:
            real = cand.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        for root in roots:
            if real == root or root in real.parents:
                return real
        raise PermissionError(f"path is outside the allowed source directories: {path}")
    raise FileNotFoundError(f"not found in allowed source directories: {path}")

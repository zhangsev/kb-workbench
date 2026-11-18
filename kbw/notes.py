"""Saving and organising analysis knowledge in the LLMWiki pages directory.

The calling agent decides when to save and supplies the content and its source
evidence; this module validates it, finds the page for the same topic, merges
without losing information, writes atomically under LLMWiki's own write lock,
keeps LLMWiki's index.md and log.md up to date, and refreshes kb-workbench's
search index. Nothing here calls a model or reads chat history.

Layout inside <wiki_root>/wiki (LLMWiki's pages directory, the same one that
search_knowledge indexes):

    analyses/<service>/<slug>.md   implementation, design and troubleshooting topics
    frameworks/<slug>.md           general framework or platform usage
    hubs/<service>.md              generated navigation per service
    hubs/frameworks.md             generated navigation for framework pages
    .kbw/registry.json             stable ids, redirects of moved/merged pages,
                                   request ids (not a page; LLMWiki ignores it)

Every managed page is a normal LLMWiki page (type, title, tags, created,
updated) with its structured content under the frontmatter key `kbw`; the body
is rendered from it. A body edited by hand is detected and never overwritten
silently.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml

from .config import Config
from .util import fts_tokens, git, git_state, log, sha

KINDS = {
    "implementation": "现有实现",
    "design": "设计方案（未实现）",
    "troubleshooting": "排障结论",
    "framework": "框架通用用法",
}
ASPECTS = {"flow": "流程", "rule": "规则", "usage": "用法", "fact": "事实"}
LOCK_FILE = ".llmwiki.lock"          # same file and protocol as LLMWiki's withWikiLock
LOCK_STALE_S, LOCK_TIMEOUT_S = 10.0, 30.0
REGISTRY = ".kbw/registry.json"
NAV_FILES = {"index.md", "log.md"}

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SERVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_COMMIT = re.compile(r"^[0-9a-fA-F]{7,40}$")
_BRANCH = re.compile(r"^[\w./+-]{1,120}$")
_ASCII_TAG = re.compile(r"[^A-Za-z0-9_-]+")
_LINK = re.compile(r"(\]\()([^)\s#]+\.md)((?:#[^)\s]*)?\))")
_SECRETS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("api key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("cookie", re.compile(r"(?i)\b(?:set-)?cookie\s*:\s*[^\s=;]+=[^\s;]{8,}")),
    ("credential assignment", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)\b"
        r"[\"']?\s*[:=]\s*[\"']?(?=[A-Za-z0-9+/=_.!@#$%^&*-]*\d)[A-Za-z0-9+/=_.!@#$%^&*-]{8,}")),
]


class KnowledgeError(ValueError):
    """Invalid input; nothing was written."""


class _Q(str):
    """A string always written quoted, so YAML readers keep timestamps as text."""


yaml.SafeDumper.add_representer(_Q, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", str(v), style="'"))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", str(text)).casefold()
    t = re.sub(r"\s+", " ", t).strip()
    return t.rstrip("。.;；,，:：!！ ")


def _version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()[:16]


def _rel(target: str, from_page: str) -> str:
    return os.path.relpath(target, os.path.dirname(from_page) or ".").replace(os.sep, "/")


def _resolve_link(link: str, from_page: str) -> str | None:
    if "://" in link or link.startswith(("/", "mailto:")):
        return None
    joined = os.path.normpath(os.path.join(os.path.dirname(from_page), link)).replace(os.sep, "/")
    return None if joined.startswith("..") else joined


def _tag(text: str) -> str:
    return _ASCII_TAG.sub("-", str(text)).strip("-")[:40]


def _tokens(text: str) -> set[str]:
    return {t for t in fts_tokens(text or "") if len(t) > 1}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


# ------------------------------------------------------------------ files and lock

def atomic_write(path: Path, text: str) -> None:
    """Write to a temp file in the same directory, fsync, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".kbw-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: target briefly open elsewhere
                if attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class WikiLock:
    """The same cross-process lock LLMWiki uses (wiki/.llmwiki.lock created with
    O_EXCL, heartbeat by touching it, considered stale after 10 s without a
    heartbeat). LLMWiki writers and this module therefore never interleave."""

    _local = threading.Lock()

    def __init__(self, wiki_dir: Path, timeout: float = LOCK_TIMEOUT_S):
        self.path = wiki_dir / LOCK_FILE
        self.timeout = timeout

    @contextmanager
    def hold(self):
        with self._local:
            self._acquire()
            stop = threading.Event()
            beat = threading.Thread(target=self._heartbeat, args=(stop,), daemon=True)
            beat.start()
            try:
                yield
            finally:
                stop.set()
                beat.join(2)
                self._release()

    def _acquire(self) -> None:
        deadline, delay = time.monotonic() + self.timeout, 0.005
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return
            except FileExistsError:
                pass
            except PermissionError:
                if os.name != "nt" or not self.path.exists():
                    raise
            try:
                if time.time() - self.path.stat().st_mtime > LOCK_STALE_S:
                    self.path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"another wiki write is still running (lock {self.path}); try again. "
                                   "If no writer is running, delete that file.")
            time.sleep(delay)
            delay = min(delay * 2, 0.1)

    def _heartbeat(self, stop: threading.Event) -> None:
        while not stop.wait(LOCK_STALE_S / 4):
            try:
                os.utime(self.path)
            except OSError:
                pass

    def _release(self) -> None:
        for attempt in range(5):
            try:
                self.path.unlink(missing_ok=True)
                return
            except PermissionError:
                time.sleep(0.02 * (attempt + 1))


# ------------------------------------------------------------------ page io

def split_page(text: str) -> tuple[dict, str]:
    text = text.replace("\r\n", "\n")
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            try:
                fm = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError:
                fm = {}
            rest = text[end + 4:]
            return (fm if isinstance(fm, dict) else {}), rest.split("\n", 1)[1] if "\n" in rest else ""
    return {}, text


def join_page(fm: dict, body: str) -> str:
    def quote(v):
        if isinstance(v, dict):
            return {k: quote(x) for k, x in v.items()}
        if isinstance(v, list):
            return [quote(x) for x in v]
        return _Q(v) if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v) else v
    head = yaml.safe_dump(quote(fm), allow_unicode=True, sort_keys=False, width=1000)
    return f"---\n{head}---\n\n{body.strip()}\n"


# ------------------------------------------------------------------ index.md / log.md (LLMWiki format)

_ENTRY = re.compile(r"^-\s+\[((?:[^\]\\]|\\.)+)\]\(((?:[^)\\]|\\.)+)\)(.*)$")


def _read_index(path: Path) -> list[dict]:
    entries, category = [], ""
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        m = re.match(r"^##\s+(.+)$", s)
        if m:
            category = m.group(1).strip()
            continue
        m = _ENTRY.match(s) if category else None
        if m:
            rest = m.group(3).strip()
            rest = re.sub(r"^[—–-]\s*", "", rest)
            tags = re.findall(r"#([\w-]+)", rest, flags=re.ASCII)
            entries.append({"title": re.sub(r"\\([\[\]()])", r"\1", m.group(1)), "path": m.group(2),
                            "summary": re.sub(r"#[\w-]+", "", rest, flags=re.ASCII).strip(),
                            "category": category, "tags": tags})
    return entries


def _write_index(path: Path, entries: list[dict]) -> None:
    cats: dict[str, list] = {}
    for e in entries:
        cats.setdefault(e["category"], []).append(e)
    out = "# Wiki Index\n"
    for cat, items in cats.items():
        out += f"\n## {cat}\n\n"
        for e in items:
            line = "- [" + re.sub(r"([\[\]()])", r"\\\1", e["title"]) + f"]({e['path']})"
            parts = [p for p in (e.get("summary") or "",
                                 " ".join(f"#{t}" for t in e.get("tags") or [])) if p]
            out += line + (" — " + " ".join(parts) if parts else "") + "\n"
    atomic_write(path, out)


def _category(rel: str) -> str:
    first = rel.split("/")[0] if "/" in rel else ""
    return first[:1].upper() + first[1:] if first else "General"


def _append_log(wiki: Path, verb: str, subject: str, details: str) -> None:
    with open(wiki / "log.md", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"## [{datetime.now().date().isoformat()}] {verb} | {subject}\n\n{details}\n\n")


# ------------------------------------------------------------------ the store

class Notes:
    def __init__(self, cfg: Config, knowledge, lock_timeout: float = LOCK_TIMEOUT_S):
        self.cfg = cfg
        self.knowledge = knowledge            # kbw.knowledge.Knowledge, for the search index
        self.lock_timeout = lock_timeout

    # .............................................................. basics

    @property
    def wiki(self) -> Path:
        if not self.cfg.wiki_pages_dir:
            raise KnowledgeError("knowledge.wiki_root is not configured")
        return self.cfg.wiki_pages_dir

    def _require_writable(self) -> None:
        if not self.cfg.knowledge_writable:
            raise PermissionError("knowledge writing is disabled (knowledge.writable: false)")
        wiki = self.wiki.resolve()
        for d in self.cfg.source_dirs:
            d = d.resolve()
            if wiki == d or d in wiki.parents:
                raise PermissionError(f"wiki pages directory {wiki} lies inside the raw source directory {d}")

    def _lock(self) -> WikiLock:
        self.wiki.mkdir(parents=True, exist_ok=True)
        return WikiLock(self.wiki, self.lock_timeout)

    def _target(self, rel: str) -> Path:
        """Absolute path of a page, refusing anything outside the pages directory,
        inside a raw source directory, or reached through a symlink."""
        rel = rel.replace("\\", "/").lstrip("/")
        if rel.startswith("wiki/"):
            rel = rel[5:]
        p = PurePosixPath(rel)
        if not rel.endswith(".md") or ".." in p.parts or rel in NAV_FILES or p.parts[0].startswith("."):
            raise KnowledgeError(f"invalid page path: {rel}")
        full = (self.wiki / rel)
        root = self.wiki.resolve()
        parent = full.parent
        while not parent.exists():
            parent = parent.parent
        if root != parent.resolve() and root not in parent.resolve().parents:
            raise PermissionError(f"page path escapes the wiki directory: {rel}")
        if full.exists() and full.is_symlink():
            raise PermissionError(f"refusing to write through a symlink: {rel}")
        for d in self.cfg.source_dirs:
            if d.resolve() == full.resolve().parent or d.resolve() in full.resolve().parents:
                raise PermissionError("knowledge tools never write into raw source directories")
        return full

    def _registry(self) -> dict:
        p = self.wiki / REGISTRY
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for k in ("ids", "moved", "requests"):
            data.setdefault(k, {})
        return data

    def _save_registry(self, reg: dict) -> None:
        reqs = reg["requests"]
        if len(reqs) > 2000:
            reg["requests"] = dict(list(reqs.items())[-2000:])
        atomic_write(self.wiki / REGISTRY, json.dumps(reg, ensure_ascii=False, indent=1, sort_keys=True))

    def resolve(self, ref: str, reg: dict | None = None) -> tuple[str, str | None]:
        """Page path for an id or a path, following moves and merges.
        Returns (current path, the reference it was redirected from or None)."""
        reg = reg or self._registry()
        ref = str(ref).strip().replace("\\", "/")
        if ref.startswith("wiki/"):
            ref = ref[5:]
        if ref in reg["ids"]:
            path = reg["ids"][ref]
            return path, None
        seen, path, redirected = set(), ref, None
        while path in reg["moved"] and path not in seen:
            seen.add(path)
            redirected = redirected or ref
            path = reg["moved"][path]
        return path, redirected

    def _read(self, rel: str) -> tuple[dict, str, bytes] | None:
        full = self.wiki / rel
        if not full.is_file():
            return None
        raw = full.read_bytes()
        fm, body = split_page(raw.decode("utf-8", errors="replace"))
        return fm, body, raw

    def catalog(self) -> list[dict]:
        """All pages: path, title, frontmatter (managed pages carry `kbw`)."""
        out = []
        for p in sorted(self.wiki.rglob("*.md")):
            rel = p.relative_to(self.wiki).as_posix()
            if any(part.startswith(".") for part in PurePosixPath(rel).parts):
                continue
            try:
                fm, body = split_page(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            out.append({"path": rel, "title": fm.get("title") or p.stem, "fm": fm, "body": body})
        return out

    # .............................................................. validation

    def _validate(self, req: dict, creating: bool, existing: dict | None) -> tuple[dict, list[str]]:
        warnings: list[str] = []
        bad: list[str] = []
        repos = {r.name: r for r in self.cfg.repos}

        def strs(name, items, maxlen=2000):
            out = []
            for i, x in enumerate(items or []):
                if not isinstance(x, str) or not x.strip():
                    bad.append(f"{name}[{i}] must be a non-empty string")
                elif len(x) > maxlen:
                    bad.append(f"{name}[{i}] longer than {maxlen} characters")
                else:
                    out.append(x.strip())
            return out

        kind = req.get("kind") or (existing or {}).get("kind")
        if kind not in KINDS:
            bad.append(f"kind must be one of {list(KINDS)}")
        title = (req.get("title") or "").strip()
        if creating and not title:
            bad.append("title is required")
        if len(title) > 120:
            bad.append("title longer than 120 characters; use a stable topic title, not the question")
        summary = (req.get("summary") or "").strip()
        if creating and not summary:
            bad.append("summary is required")
        services = strs("services", req.get("services"), 64)
        for s in services:
            if not _SERVICE.match(s):
                bad.append(f"service name not allowed: {s!r} (letters, digits, . _ -)")
            elif s not in repos:
                warnings.append(f"service {s} is not a configured repository")
        if creating and not services:
            bad.append("services must name at least one service")
        keywords = strs("keywords", req.get("keywords"), 60)
        if creating and not keywords:
            bad.append("keywords must contain at least one keyword")
        slug = req.get("slug")
        if slug is not None and not _SLUG.match(str(slug)):
            bad.append("slug must be lower-case ascii words joined by '-', e.g. alarm-push-cloudsop-storage")

        evidence, refs = [], {}
        for i, e in enumerate(req.get("evidence") or []):
            if not isinstance(e, dict):
                bad.append(f"evidence[{i}] must be an object")
                continue
            repo = str(e.get("repo") or "").strip()
            path = str(e.get("path") or "").strip().replace("\\", "/")
            if not repo or not _SERVICE.match(repo):
                bad.append(f"evidence[{i}].repo is required (repository name)")
            if (not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path)
                    or ".." in PurePosixPath(path).parts or len(path) > 300):
                bad.append(f"evidence[{i}].path must be a relative path inside the repository")
            lines = e.get("lines")
            if isinstance(lines, int):
                lines = [lines, lines]
            if lines is not None and not (isinstance(lines, list) and len(lines) == 2
                                          and all(isinstance(n, int) and 0 < n < 10**7 for n in lines)
                                          and lines[0] <= lines[1]):
                bad.append(f"evidence[{i}].lines must be [start, end] with 1 <= start <= end")
                lines = None
            commit = e.get("commit")
            if commit is not None and not _COMMIT.match(str(commit)):
                bad.append(f"evidence[{i}].commit must be a 7-40 character hex commit id")
            branch = e.get("branch")
            if branch is not None and not _BRANCH.match(str(branch)):
                bad.append(f"evidence[{i}].branch has unsupported characters")
            item = {"repo": repo, "path": path}
            for k in ("symbol", "note"):
                if e.get(k):
                    item[k] = str(e[k]).strip()[:300]
            if lines:
                item["lines"] = lines
            if commit:
                item["commit"] = str(commit).lower()
            if branch:
                item["branch"] = str(branch)
            if e.get("dirty") is not None:
                item["dirty"] = bool(e["dirty"])
            if repo in repos and path and not bad:
                if not (repos[repo].path / path).is_file():
                    warnings.append(f"evidence[{i}] {repo}:{path} does not exist in the current checkout")
            elif repo and repo not in repos:
                warnings.append(f"evidence[{i}] repository {repo} is not configured; path not checked")
            if not commit:
                warnings.append(f"evidence[{i}] has no commit; the repository HEAD at save time is recorded instead")
            evidence.append(item)
            refs[str(e.get("ref") if e.get("ref") is not None else i)] = len(evidence) - 1
            refs.setdefault(str(i), len(evidence) - 1)

        def items(name, raw, need_evidence):
            out = []
            for i, x in enumerate(raw or []):
                if isinstance(x, str):
                    x = {"text": x}
                if not isinstance(x, dict) or not str(x.get("text") or "").strip():
                    bad.append(f"{name}[{i}].text is required")
                    continue
                ev = [str(r) for r in (x.get("evidence") or [])]
                if need_evidence and not ev:
                    bad.append(f"{name}[{i}] has no evidence; a confirmed fact needs at least one evidence ref "
                               "(put unverified statements in inferences)")
                aspect = x.get("aspect", "fact")
                if aspect not in ASPECTS:
                    bad.append(f"{name}[{i}].aspect must be one of {list(ASPECTS)}")
                out.append({"text": str(x["text"]).strip()[:2000], "aspect": aspect, "evidence": ev})
            return out

        facts = items("facts", req.get("facts"), True)
        designs = items("designs", req.get("designs"), False)
        if kind == "design" and creating and not designs:
            warnings.append("kind is design but no designs were given")
        if kind != "design" and creating and not facts:
            bad.append("an implementation, troubleshooting or framework page needs at least one confirmed fact")
        if creating and not evidence:
            bad.append("evidence must contain at least one source location")

        # every scalar the caller sent is checked for credentials
        def walk(v, where):
            if isinstance(v, str):
                for label, rx in _SECRETS:
                    if rx.search(v):
                        bad.append(f"{where} looks like it contains a credential ({label}); remove it")
                        return
            elif isinstance(v, dict):
                for k, x in v.items():
                    walk(x, f"{where}.{k}")
            elif isinstance(v, list):
                for i, x in enumerate(v):
                    walk(x, f"{where}[{i}]")
        walk(req, "request")
        if bad:
            raise KnowledgeError("; ".join(dict.fromkeys(bad)))
        clean = {
            "kind": kind, "title": title, "summary": summary, "services": services, "keywords": keywords,
            "question": (req.get("question") or "").strip()[:500] or None,
            "facts": facts, "designs": designs,
            "inferences": [{"text": t} for t in strs("inferences", req.get("inferences"))],
            "open_questions": [{"text": t} for t in strs("open_questions", req.get("open_questions"))],
            "evidence": evidence, "refs": refs,
            "tags": [t for t in (_tag(x) for x in strs("tags", req.get("tags"), 40)) if t],
            "project": (req.get("project") or self.cfg.knowledge_project or None),
        }
        return clean, warnings

    # .............................................................. merging

    @staticmethod
    def _next_id(items: list[dict], prefix: str) -> str:
        n = max([int(x["id"][len(prefix):]) for x in items if str(x.get("id", "")).startswith(prefix)
                 and x["id"][len(prefix):].isdigit()] + [0])
        return f"{prefix}{n + 1}"

    def _merge(self, cur: dict, new: dict, supersede: list[str], resolve: list[str], replace: bool,
               when: str) -> dict:
        """Fold new findings into a page's structured data. Exact duplicates
        (after normalisation) collapse; nothing is dropped silently: superseded,
        resolved or rewritten-away items move to history."""
        stats = {"added": {}, "duplicates_skipped": 0, "moved_to_history": 0, "evidence_updated": 0}
        data = json.loads(json.dumps(cur))
        for k in ("services", "keywords", "questions", "tags", "related", "facts", "designs", "inferences",
                  "open_questions", "history", "evidence", "aliases", "old_paths", "saves"):
            data.setdefault(k, [])

        def bump(k, n=1):
            stats["added"][k] = stats["added"].get(k, 0) + n

        def union(key, values):
            seen = {_norm(v) for v in data[key]}
            for v in values:
                if v and _norm(v) not in seen:
                    data[key].append(v)
                    seen.add(_norm(v))
                    bump(key)
                elif v:
                    stats["duplicates_skipped"] += 1

        def to_history(item, reason):
            data["history"].append({"id": self._next_id(data["history"], "h"), "text": item["text"],
                                    "was": item.get("_section"), "reason": reason, "at": when})
            stats["moved_to_history"] += 1

        # evidence: one entry per (repo, path, symbol, lines); a newer analysis refreshes commit info
        ev_map = {}
        if replace:
            old_ev, data["evidence"] = data["evidence"], []
        for i, e in enumerate(data["evidence"]):
            ev_map[(e["repo"], e["path"], e.get("symbol", ""), tuple(e.get("lines") or []))] = e
        new_ids = []
        for e in new["evidence"]:
            key = (e["repo"], e["path"], e.get("symbol", ""), tuple(e.get("lines") or []))
            if replace:
                prev = next((x for x in old_ev if (x["repo"], x["path"], x.get("symbol", ""),
                                                   tuple(x.get("lines") or [])) == key), None)
                if prev and key not in ev_map:
                    e = {**e, "id": prev["id"]}
            if key in ev_map:
                cur_e = ev_map[key]
                changed = {k: v for k, v in e.items() if k != "id" and cur_e.get(k) != v}
                if changed:
                    cur_e.update(changed)
                    stats["evidence_updated"] += 1
                else:
                    stats["duplicates_skipped"] += 1
                new_ids.append(cur_e["id"])
            else:
                item = {"id": e.get("id") or self._next_id(data["evidence"] + (old_ev if replace else []), "e"), **e}
                item.pop("ref", None)
                data["evidence"].append(item)
                ev_map[key] = item
                new_ids.append(item["id"])
                bump("evidence")
        known = {e["id"] for e in data["evidence"]}

        def map_refs(refs):
            out = []
            for r in refs:
                if r in new["refs"]:
                    out.append(new_ids[new["refs"][r]])
                elif r in known:
                    out.append(r)
                else:
                    raise KnowledgeError(f"evidence ref {r!r} matches neither a submitted evidence ref nor an "
                                         f"evidence id on the page ({sorted(known)})")
            return list(dict.fromkeys(out))

        # text sections
        sections = [("facts", "f"), ("designs", "d"), ("inferences", "i"), ("open_questions", "q")]
        if replace:
            for key, _ in sections:
                newkeys = {_norm(x["text"]) for x in new[key]}
                keep = []
                for x in data[key]:
                    if _norm(x["text"]) in newkeys:
                        keep.append(x)
                    else:
                        to_history({**x, "_section": key}, "removed in a curated rewrite")
                data[key] = keep
        answered = {_norm(h["text"]) for h in data["history"] if h.get("reason") == "answered"}
        for key, prefix in sections:
            index = {_norm(x["text"]): x for x in data[key]}
            for x in new[key]:
                ev = map_refs(x.get("evidence", []))
                if key == "open_questions" and _norm(x["text"]) in answered:
                    stats["already_answered"] = stats.get("already_answered", []) + [x["text"]]
                    continue
                hit = index.get(_norm(x["text"]))
                if hit:
                    before = list(hit.get("evidence", []))
                    if ev:
                        hit["evidence"] = list(dict.fromkeys(before + ev))
                    if key in ("facts", "designs") and x.get("aspect") and hit.get("aspect") != x["aspect"]:
                        hit["aspect"] = x["aspect"]
                    if hit.get("evidence", []) == before:
                        stats["duplicates_skipped"] += 1
                    continue
                item = {"id": self._next_id(data[key], prefix), "text": x["text"]}
                if key in ("facts", "designs"):
                    item.update(aspect=x.get("aspect", "fact"), evidence=ev)
                data[key].append(item)
                index[_norm(x["text"])] = item
                bump(key)
        # a statement cannot be both confirmed and merely inferred / proposed
        confirmed = {_norm(x["text"]) for x in data["facts"]}
        for key in ("inferences", "designs"):
            keep = []
            for x in data[key]:
                if _norm(x["text"]) in confirmed:
                    to_history({**x, "_section": key}, "now confirmed in code" if key == "inferences"
                               else "now implemented; recorded as a confirmed fact")
                else:
                    keep.append(x)
            data[key] = keep

        # explicit supersede / resolve
        def take(ref_list, keys, reason):
            for ref in ref_list:
                found = False
                for key in keys:
                    for x in list(data[key]):
                        if x.get("id") == ref or _norm(x["text"]) == _norm(ref):
                            data[key].remove(x)
                            to_history({**x, "_section": key}, reason)
                            found = True
                if not found:
                    raise KnowledgeError(f"nothing on the page matches {ref!r} (use an item id such as f2 or its text)")
        take(supersede, ["facts", "designs", "inferences"], "superseded")
        take(resolve, ["open_questions"], "answered")

        # scalar and list metadata
        if replace:
            for key in ("services", "keywords", "tags"):
                data[key] = []
        union("services", new["services"])
        union("keywords", new["keywords"])
        union("tags", new["tags"])
        if new.get("question"):
            union("questions", [new["question"]])
            data["questions"] = data["questions"][-30:]
        if new["summary"] and new["summary"] != data.get("summary"):
            data["summary"] = new["summary"]
            stats["summary_changed"] = True
        if new.get("project") and not data.get("project"):
            data["project"] = new["project"]
        if replace and new["kind"]:
            data["kind"] = new["kind"]
        data["_stats"] = stats
        return data

    # .............................................................. rendering

    def _render(self, rel: str, data: dict, title: str, titles: dict[str, tuple[str, str]]) -> str:
        """Markdown body from structured data. `titles` maps page id -> (path, title)."""
        ev_ids = {e["id"]: e for e in data["evidence"]}

        def refs(ids):
            return " " + "".join(f"[{i}]" for i in ids) if ids else ""

        kind_label = KINDS[data["kind"]]
        lines = [f"# {title}", "",
                 f"> 内容类型：{kind_label} · 关联服务：{', '.join(data['services'])}"
                 + (f" · 项目：{data['project']}" if data.get("project") else ""),
                 f"> 由 kb-workbench 维护（ID：{data['id']}）。请通过 save_knowledge 更新；"
                 "直接修改正文会使下次自动保存被拒绝，以免覆盖手工内容。", ""]
        lines += ["## 概述", "", data.get("summary") or "（无）", ""]
        lines += ["## 关键词与常见问法", "", "- 关键词：" + "、".join(data["keywords"])]
        lines += [f"- 问法：{q}" for q in data["questions"]] + [""]
        if data["kind"] == "design":
            sec_facts, sec_design = "## 现有实现（设计所基于的现状）", "## 设计方案（尚未实现，不代表现有行为）"
        elif data["kind"] == "framework":
            sec_facts, sec_design = "## 已确认的用法", "## 待实施方案（尚未实现）"
        else:
            sec_facts, sec_design = "## 已确认的现有实现", "## 待实施方案（尚未实现）"
        if data["facts"]:
            lines += [sec_facts, ""]
            for aspect, label in ASPECTS.items():
                group = [f for f in data["facts"] if f.get("aspect", "fact") == aspect]
                if not group:
                    continue
                lines += [f"### {label}", ""]
                for n, f in enumerate(group, 1):
                    bullet = f"{n}." if aspect == "flow" else "-"
                    lines.append(f"{bullet} {f['text']}{refs(f.get('evidence'))} `{f['id']}`")
                lines.append("")
        if data["designs"]:
            lines += [sec_design, ""] + [f"- {d['text']}{refs(d.get('evidence'))} `{d['id']}`"
                                         for d in data["designs"]] + [""]
        if data["inferences"]:
            lines += ["## 推断（未经代码直接确认）", ""] + [f"- {x['text']} `{x['id']}`" for x in data["inferences"]] + [""]
        if data["open_questions"]:
            lines += ["## 待确认", ""] + [f"- {x['text']} `{x['id']}`" for x in data["open_questions"]] + [""]
        if data["history"]:
            reason = {"superseded": "已被取代", "answered": "已解答", "removed in a curated rewrite": "整理时移除",
                      "now confirmed in code": "已由代码确认", "now implemented; recorded as a confirmed fact": "已实现"}
            lines += ["## 历史结论", ""] + [
                f"- {h['text']}（{reason.get(h['reason'], h['reason'])}，{str(h['at'])[:10]}）" for h in data["history"][-40:]] + [""]
        lines += ["## 源码依据", "", "| ID | 仓库 | 路径 | 类/方法 | 行 | 分支 | 提交 | 未提交修改 |",
                  "|---|---|---|---|---|---|---|---|"]
        for e in data["evidence"]:
            ln = e.get("lines")
            obs = (data.get("observed") or {}).get(e["repo"], {})
            dirty = "是" if e.get("dirty") else ("否" if e.get("dirty") is False else "未注明")
            commit = e.get("commit") or (f"（未提供，保存时 HEAD {obs['head'][:10]}）" if obs.get("head") else "（未提供）")
            lines.append(f"| {e['id']} | {e['repo']} | `{e['path']}` | {e.get('symbol', '')} | "
                         f"{f'{ln[0]}-{ln[1]}' if ln else ''} | {e.get('branch', '')} | {commit} | {dirty} |")
        lines.append("")
        if data.get("observed"):
            lines += ["## 保存时的仓库状态", "", "| 仓库 | HEAD | 工作区有未提交修改 | 时间 |", "|---|---|---|---|"]
            for repo, o in data["observed"].items():
                lines.append(f"| {repo} | {(o.get('head') or '未知')[:12]} | "
                             f"{'是' if o.get('dirty') else '否' if o.get('dirty') is False else '未知'} | {str(o.get('at'))[:19]} |")
            lines.append("")
        related = [(titles[r][0], titles[r][1]) for r in data["related"] if r in titles]
        hubs = [f"hubs/{s}.md" for s in data["services"]] + (["hubs/frameworks.md"] if data["kind"] == "framework" else [])
        lines += ["## 相关页面", ""] + [f"- [{t}]({_rel(p, rel)})" for p, t in related]
        lines += [f"- [导航：{PurePosixPath(h).stem}]({_rel(h, rel)})" for h in hubs] + [""]
        lines += ["## 变更记录", ""] + [f"- {str(s['at'])[:19]} {s['action']}" + (f"：{s['note']}" if s.get("note") else "")
                                          for s in data["saves"][-30:]]
        return "\n".join(lines)

    def _frontmatter(self, data: dict, title: str, created: str, updated: str) -> dict:
        tags = list(dict.fromkeys([data["kind"]] + [_tag(s) for s in data["services"]] + data["tags"]))
        return {"type": "concept", "title": title, "tags": [t for t in tags if t], "created": created,
                "updated": updated, "kbw": {k: v for k, v in data.items() if not k.startswith("_")}}

    def _write_page(self, rel: str, data: dict, title: str, created: str, reg: dict, catalog: list[dict]) -> bytes:
        titles = self._titles(reg, catalog)
        body = self._render(rel, data, title, titles)
        data["body_hash"] = _body_hash(body)
        text = join_page(self._frontmatter(data, title, created, _now()), body)
        atomic_write(self._target(rel), text)
        return text.encode("utf-8")

    def _titles(self, reg: dict, catalog: list[dict]) -> dict[str, tuple[str, str]]:
        out = {}
        for c in catalog:
            out[c["path"]] = (c["path"], c["title"])   # related pages not managed here are referenced by path
            k = c["fm"].get("kbw")
            if isinstance(k, dict) and k.get("id"):
                out[k["id"]] = (c["path"], c["title"])
                for a in k.get("aliases") or []:
                    out[a] = (c["path"], c["title"])
        return out

    # .............................................................. navigation

    def _refresh_navigation(self, catalog: list[dict], services: set[str], frameworks: bool,
                            removed: list[str] = (), renamed: dict[str, str] | None = None) -> list[str]:
        """Regenerate hub pages for the given services and upsert index.md entries
        for every managed page and hub (LLMWiki's index format)."""
        touched = []
        managed = [c for c in catalog if isinstance(c["fm"].get("kbw"), dict)]
        hubs = {f"hubs/{s}.md": ("service", s) for s in services}
        if frameworks:
            hubs["hubs/frameworks.md"] = ("frameworks", None)
        for rel, (kind, svc) in hubs.items():
            pages = [c for c in managed if (svc in (c["fm"]["kbw"].get("services") or [])
                                            if kind == "service" else c["fm"]["kbw"].get("kind") == "framework")]
            title = f"导航：{svc}" if svc else "导航：框架通用用法"
            body = [f"# {title}", "", "> 由 kb-workbench 自动生成，请勿手工编辑。", ""]
            for k, label in KINDS.items():
                group = [c for c in pages if c["fm"]["kbw"].get("kind") == k]
                if group:
                    body += [f"## {label}", ""] + [
                        f"- [{c['title']}]({_rel(c['path'], rel)}) — {(c['fm']['kbw'].get('summary') or '')[:80]}"
                        for c in sorted(group, key=lambda c: c["title"])] + [""]
            if not pages:
                body += ["（暂无页面）", ""]
            fm = {"type": "concept", "title": title, "tags": ["hub"] + ([_tag(svc)] if svc else []),
                  "created": _now(), "updated": _now(), "kbw_hub": svc or "frameworks"}
            old = self._read(rel)
            if old:
                fm["created"] = old[0].get("created", fm["created"])
                if split_page(old[2].decode("utf-8"))[1].strip() == "\n".join(body).strip():
                    continue
            atomic_write(self._target(rel), join_page(fm, "\n".join(body)))
            touched.append(rel)
            catalog = [c for c in catalog if c["path"] != rel] + [{"path": rel, "title": title, "fm": fm, "body": ""}]
        idx_path = self.wiki / "index.md"
        original = _read_index(idx_path)
        entries = []
        for e in original:
            if e["path"] in removed:
                continue
            if renamed and e["path"] in renamed:
                e = {**e, "path": renamed[e["path"]]}
            if any(x["path"] == e["path"] for x in entries):
                continue
            entries.append(e)
        by_path = {e["path"]: e for e in entries}
        changed = entries != original
        for c in catalog:
            fm = c["fm"]
            if not (isinstance(fm.get("kbw"), dict) or fm.get("kbw_hub")):
                continue
            k = fm.get("kbw") or {}
            want = {"title": c["title"], "path": c["path"], "category": _category(c["path"]),
                    "summary": (k.get("summary") or "")[:80].replace("\n", " "),
                    "tags": [t for t in (fm.get("tags") or []) if re.fullmatch(r"[\w-]+", t, flags=re.ASCII)]}
            cur = by_path.get(c["path"])
            if cur is None:
                entries.append(want)
                by_path[c["path"]] = want
                changed = True
            elif any(cur.get(x) != want[x] for x in ("title", "summary", "tags", "category")):
                cur.update(want)
                changed = True
        if changed:
            _write_index(idx_path, entries)
            touched.append("index.md")
        return touched

    # .............................................................. find

    def find(self, title: str = "", services: list[str] | None = None, keywords: list[str] | None = None,
             evidence_paths: list[str] | None = None, question: str = "", slug: str | None = None,
             kind: str | None = None, limit: int = 8, _catalog: list[dict] | None = None) -> dict:
        """Candidate pages for the same topic. Scores only point at candidates;
        the caller decides whether a candidate is the same topic."""
        catalog = _catalog if _catalog is not None else self.catalog()
        services = list(services or [])
        want_title = _tokens(title)
        want_kw = {_norm(k) for k in keywords or []}
        want_q = _tokens(question)
        want_ev = {p.replace("\\", "/") for p in evidence_paths or []}
        exact = None
        if slug and kind:
            exact = self._path_for(kind, services, slug)
        out = []
        for c in catalog:
            if c["path"].startswith("hubs/") or c["path"] in NAV_FILES:
                continue
            k = c["fm"].get("kbw") if isinstance(c["fm"].get("kbw"), dict) else {}
            reasons, score = [], 0.0
            if exact and c["path"] == exact:
                reasons.append("same path (same kind, primary service and slug)")
                score = 1.0
            ts = _jaccard(want_title, _tokens(c["title"]))
            if ts >= 0.2:
                reasons.append(f"title similarity {ts:.2f}")
            kw = _jaccard(want_kw, {_norm(x) for x in k.get("keywords") or []})
            if kw:
                reasons.append(f"keyword overlap {kw:.2f}")
            sv = set(services) & set(k.get("services") or [])
            if sv:
                reasons.append(f"shared services {sorted(sv)}")
            evp = {f"{e['repo']}:{e['path']}" for e in k.get("evidence") or []} | {e["path"] for e in k.get("evidence") or []}
            ev = len(want_ev & evp) / len(want_ev) if want_ev else 0.0
            if ev:
                reasons.append(f"shared source files {ev:.2f}")
            qs = max([_jaccard(want_q, _tokens(q)) for q in k.get("questions") or []] + [0.0]) if want_q else 0.0
            if qs >= 0.3:
                reasons.append(f"similar earlier question {qs:.2f}")
            score = max(score, 0.4 * ts + 0.2 * kw + 0.1 * bool(sv) + 0.2 * ev + 0.1 * qs)
            if not k:  # pages not managed by kb-workbench: only title and text hints
                body_hit = sum(1 for t in want_title if t in _norm(c["body"])) / len(want_title) if want_title else 0
                score = max(score, 0.3 * ts + 0.2 * body_hit)
            if score >= 0.15 or (exact and c["path"] == exact):
                out.append({"page_id": k.get("id"), "path": c["path"], "title": c["title"],
                            "kind": k.get("kind"), "services": k.get("services") or [],
                            "managed": bool(k), "version": _version((self.wiki / c["path"]).read_bytes()),
                            "score": round(score, 3), "reasons": reasons,
                            "strong": score >= 0.45 and (bool(sv) or not services or not k) or score == 1.0})
        out.sort(key=lambda x: -x["score"])
        return {"candidates": out[:limit],
                "note": "Scores only find candidates. Same topic -> update that page (page_id). "
                        "Different sub-topic -> create a new page with distinct_from=[ids] so the pages get linked."}

    def _path_for(self, kind: str, services: list[str], slug: str) -> str:
        if kind == "framework":
            return f"frameworks/{slug}.md"
        if not services:
            raise KnowledgeError("services is required to place the page")
        return f"analyses/{services[0]}/{slug}.md"

    # .............................................................. save

    def save(self, req: dict) -> dict:
        self._require_writable()
        mode = req.get("mode") or "merge"
        if mode not in ("merge", "replace"):
            raise KnowledgeError("mode must be merge or replace")
        request_id = (req.get("request_id") or "").strip()[:100] or None
        when = _now()
        with self._lock().hold():
            reg = self._registry()
            if request_id and request_id in reg["requests"]:
                pid = reg["requests"][request_id]
                path, _ = self.resolve(pid, reg)
                return {"status": "duplicate_request", "page_id": pid, "path": path,
                        "version": _version((self.wiki / path).read_bytes()) if (self.wiki / path).exists() else None,
                        "message": "this request_id was already saved; nothing written",
                        "index": {"status": "unchanged"}, "searchable": None}
            catalog = self.catalog()
            existing_rel, redirected = None, None
            if req.get("page_id"):
                existing_rel, redirected = self.resolve(req["page_id"], reg)
                if not (self.wiki / existing_rel).is_file():
                    raise KnowledgeError(f"page not found: {req['page_id']}")
            cur_page = self._read(existing_rel) if existing_rel else None
            cur_kbw = cur_page[0].get("kbw") if cur_page else None
            if cur_page and not isinstance(cur_kbw, dict):
                raise KnowledgeError(f"{existing_rel} is not managed by kb-workbench; "
                                     "save a new page and link it with related=[...]")
            creating = cur_page is None
            clean, warnings = self._validate(req, creating=creating, existing=cur_kbw)
            if creating:
                slug = req.get("slug")
                if not slug:
                    raise KnowledgeError("slug is required to create a page (or pass page_id to update one)")
                rel = self._path_for(clean["kind"], clean["services"], slug)
                cur_page = self._read(rel)
                if cur_page:
                    cur_kbw = cur_page[0].get("kbw")
                    if not isinstance(cur_kbw, dict):
                        raise KnowledgeError(f"{rel} already exists and is not managed by kb-workbench")
                    creating = False      # same kind + service + slug = same topic: update in place
                    existing_rel = rel
                    warnings.append(f"page {rel} already exists for this slug; updating it instead of creating")
            if creating:
                acknowledged = set(req.get("distinct_from") or [])
                cands = self.find(clean["title"], clean["services"], clean["keywords"],
                                  [f"{e['repo']}:{e['path']}" for e in clean["evidence"]], clean["question"] or "",
                                  _catalog=catalog)["candidates"]
                blocking = [c for c in cands if c["strong"] and c["page_id"] not in acknowledged
                            and c["path"] not in acknowledged]
                if blocking:
                    return {"status": "needs_decision", "written": False, "candidates": blocking,
                            "message": "similar pages exist. If one is the same topic, save again with its page_id. "
                                       "If this is a different sub-topic, save again with distinct_from=[their ids] "
                                       "(the pages will be linked).", "index": {"status": "skipped"}, "searchable": False}
                for ref in acknowledged:
                    p, _ = self.resolve(ref, reg)
                    if not (self.wiki / p).is_file():
                        raise KnowledgeError(f"distinct_from page not found: {ref}")
                page_id = "kb-" + secrets.token_hex(5)
                cur_data = {"id": page_id, "kind": clean["kind"]}
                created = when
                title = clean["title"]
                related_new = list(acknowledged)
            else:
                rel = existing_rel
                fm, body, raw = cur_page
                if mode == "replace":
                    if not req.get("expected_version"):
                        raise KnowledgeError("replace needs expected_version (from read_page or find_knowledge)")
                if req.get("expected_version") and req["expected_version"] != _version(raw):
                    return {"status": "conflict", "page_id": cur_kbw.get("id"), "path": rel,
                            "current_version": _version(raw), "written": False,
                            "message": "the page changed since you read it; read it again and resubmit",
                            "index": {"status": "skipped"}, "searchable": None}
                if cur_kbw.get("body_hash") and _body_hash(body) != cur_kbw["body_hash"]:
                    return {"status": "conflict", "page_id": cur_kbw.get("id"), "path": rel,
                            "current_version": _version(raw), "written": False,
                            "message": "the page body was edited outside save_knowledge; those edits would be lost. "
                                       "Fold them into the structured content (mode=replace with expected_version) "
                                       "or restore the page, then save again.",
                            "index": {"status": "skipped"}, "searchable": None}
                page_id = cur_kbw["id"]
                cur_data = cur_kbw
                created = str(fm.get("created") or when)
                title = clean["title"] if (mode == "replace" and clean["title"]) else str(fm.get("title"))
                if clean["title"] and _norm(clean["title"]) != _norm(title):
                    warnings.append(f"kept the stable title {title!r} (submitted {clean['title']!r}); "
                                    "pass mode=replace to retitle")
                if clean["kind"] and clean["kind"] != cur_kbw.get("kind") and mode != "replace":
                    warnings.append(f"kept kind {cur_kbw.get('kind')}; pass mode=replace to change it")
                    clean["kind"] = cur_kbw.get("kind")
                related_new = []
            related_refs = list(dict.fromkeys(list(req.get("related") or []) + related_new))
            related_ids = []
            for ref in related_refs:
                p, _ = self.resolve(ref, reg)
                page = self._read(p)
                if not page:
                    raise KnowledgeError(f"related page not found: {ref}")
                k = page[0].get("kbw")
                related_ids.append(k["id"] if isinstance(k, dict) else p)
            data = self._merge(cur_data, clean, list(req.get("supersede") or []), list(req.get("resolve") or []),
                               mode == "replace", when)
            stats = data.pop("_stats")
            for r in related_ids:
                if r != page_id and r not in data["related"]:
                    data["related"].append(r)
                    stats["added"]["related"] = stats["added"].get("related", 0) + 1
            data["id"], data["kind"] = page_id, data.get("kind") or clean["kind"]
            # repository state observed by the server at save time (not evidence)
            for repo in {e["repo"] for e in data["evidence"]}:
                r = self.cfg.repo(repo)
                if r and r.path.is_dir():
                    st = git_state(r.path)
                    data.setdefault("observed", {})[repo] = {"head": st["head"], "dirty": st["dirty"], "at": when}
            def comparable(d):
                d = {k: v for k, v in d.items() if k not in ("saves", "observed", "body_hash") and v not in ([], None, "")}
                return json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)
            if not creating and comparable(data) == comparable(cur_data) and title == cur_page[0].get("title"):
                if request_id:
                    reg["requests"][request_id] = page_id
                    self._save_registry(reg)
                return {"status": "unchanged", "page_id": page_id, "path": rel, "version": _version(cur_page[2]),
                        "changes": stats, "warnings": warnings, "index": {"status": "unchanged"},
                        "searchable": True, "message": "nothing new; page not rewritten"}
            note = req.get("change_note") or ", ".join(f"+{n} {k}" for k, n in stats["added"].items()) \
                or ("summary updated" if stats.get("summary_changed") else "metadata refreshed")
            data["saves"] = (data.get("saves") or []) + [
                {"at": when, "action": "创建" if creating else ("整理重写" if mode == "replace" else "合并更新"),
                 "note": str(note)[:200], **({"request": request_id} if request_id else {})}]
            data["saves"] = data["saves"][-50:]
            prev_version = None if creating else _version(cur_page[2])
            raw = self._write_page(rel, data, title, created, reg, catalog)
            reg["ids"][page_id] = rel
            if request_id:
                reg["requests"][request_id] = page_id
            self._save_registry(reg)
            # back-links on related managed pages (explicit relations only)
            catalog = self.catalog()
            back = []
            for rid in data["related"]:
                p, _ = self.resolve(rid, reg)
                page = self._read(p)
                if not page or not isinstance(page[0].get("kbw"), dict):
                    continue
                k = page[0]["kbw"]
                if page_id in (k.get("related") or []):
                    continue
                if k.get("body_hash") and _body_hash(page[1]) != k["body_hash"]:
                    warnings.append(f"did not add a back-link to {p}: its body was edited by hand")
                    continue
                k.setdefault("related", []).append(page_id)
                k.setdefault("saves", []).append({"at": when, "action": "关联", "note": f"linked to {page_id}"})
                self._write_page(p, k, str(page[0].get("title")), str(page[0].get("created") or when), reg, catalog)
                back.append(p)
            catalog = self.catalog()
            services = set(data["services"]) | set(cur_data.get("services") or [])
            nav = self._refresh_navigation(catalog, services, data["kind"] == "framework")
            _append_log(self.wiki, "created" if creating else "updated", rel,
                        f"kb-workbench save_knowledge ({page_id}): {note}")
        index = self.reindex([rel] + back + nav)
        return {"status": "created" if creating else "updated", "page_id": page_id, "path": rel,
                "redirected_from": redirected, "title": title, "version": _version(raw),
                "previous_version": prev_version, "changes": stats, "warnings": warnings,
                "related_updated": back, "navigation_updated": nav, "index": index,
                "searchable": index["status"] == "indexed"}

    # .............................................................. index

    def reindex(self, pages: list[str] | None = None) -> dict:
        """Refresh kb-workbench's search index from the files. A failure leaves the
        saved pages in place and is recorded as pending until a retry succeeds."""
        store = self.knowledge.store
        pending = store.get_meta("pending_reindex", [])
        try:
            res = self.knowledge.sync()
        except Exception as exc:  # the page is saved; only its searchability is in question
            log.warning("knowledge index refresh failed: %s", exc)
            merged = {p["path"]: p for p in pending}
            for p in pages or []:
                merged[p] = {"path": p, "error": f"{type(exc).__name__}: {exc}", "at": _now()}
            store.set_meta("pending_reindex", list(merged.values()))
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "pending": sorted(merged),
                    "retry": "call reindex_knowledge (or `python -m kbw index --knowledge-only`)"}
        if pending:
            store.set_meta("pending_reindex", [])
        return {"status": "indexed", "pages_updated": res["updated"], "pages_removed": res["removed"],
                "semantic": res["semantic"], "recovered": sorted(p["path"] for p in pending)}

    def pending(self) -> list[dict]:
        return self.knowledge.store.get_meta("pending_reindex", [])

    # .............................................................. source freshness

    def source_check(self, data: dict) -> dict:
        """Has the code behind a page changed since it was recorded? A change is a
        signal to re-check, never proof that the page is wrong."""
        out = []
        for e in data.get("evidence") or []:
            r = self.cfg.repo(e["repo"])
            item = {"id": e["id"], "repo": e["repo"], "path": e["path"]}
            if not r or not r.path.is_dir():
                item["status"] = "repo_not_configured"
                out.append(item)
                continue
            base = e.get("commit") or ((data.get("observed") or {}).get(e["repo"]) or {}).get("head")
            head = git(r.path, "rev-parse", "HEAD")
            item["recorded_commit"], item["current_commit"] = base, head
            if not (r.path / e["path"]).exists():
                item["status"] = "file_missing_now"
            elif not base:
                item["status"] = "unknown_base"
            elif git(r.path, "cat-file", "-e", f"{base}^{{commit}}") is None:
                item["status"] = "recorded_commit_not_found"
            else:
                diff = git(r.path, "diff", "--name-only", base, "HEAD", "--", e["path"])
                item["status"] = "unknown" if diff is None else ("changed_since_recorded" if diff else "unchanged")
            local = git(r.path, "status", "--porcelain", "--", e["path"])
            if local:
                item["uncommitted_changes_now"] = True
            if e.get("dirty"):
                item["recorded_with_uncommitted_changes"] = True
            out.append(item)
        flags = {i["status"] for i in out}
        summary = ("needs_review" if flags & {"changed_since_recorded", "file_missing_now", "recorded_commit_not_found"}
                   or any(i.get("uncommitted_changes_now") or i.get("recorded_with_uncommitted_changes") for i in out)
                   else "unchanged" if flags == {"unchanged"} else "unknown")
        return {"summary": summary, "evidence": out,
                "note": "A source change only means the conclusion should be re-checked against the current code; "
                        "it neither proves the page wrong nor right."}

    # .............................................................. organise: move / merge

    def organize(self, action: str, page: str, target: str | None = None, new_slug: str | None = None,
                 plan_token: str | None = None) -> dict:
        """Preview (no plan_token) or apply (with the plan_token from the preview)
        a move or merge. Applying refuses if any involved page changed since."""
        if action not in ("move", "merge"):
            raise KnowledgeError("action must be move or merge")
        if plan_token:
            self._require_writable()
        with self._lock().hold() if plan_token else _nullcontext():
            reg = self._registry()
            src, _ = self.resolve(page, reg)
            sp = self._read(src)
            if not sp:
                raise KnowledgeError(f"page not found: {page}")
            if src.startswith("hubs/"):
                raise KnowledgeError("hub pages are generated; they cannot be moved or merged")
            catalog = self.catalog()
            if action == "move":
                if not new_slug or not _SLUG.match(new_slug):
                    raise KnowledgeError("move needs new_slug (lower-case ascii words joined by '-')")
                dst = str(PurePosixPath(src).with_name(f"{new_slug}.md"))
                if (self.wiki / dst).exists():
                    raise KnowledgeError(f"{dst} already exists; merge instead")
                involved = {src: _version(sp[2])}
                links = self._links_to(catalog, src)
                plan = {"action": "move", "from": src, "to": dst, "pages_with_links_to_fix": links,
                        "redirect": f"{src} -> {dst} (old path and page id keep resolving)"}
            else:
                if not target:
                    raise KnowledgeError("merge needs target (the page that stays)")
                dst, _ = self.resolve(target, reg)
                tp = self._read(dst)
                if not tp or dst == src:
                    raise KnowledgeError(f"target page not found or same as source: {target}")
                sk, tk = sp[0].get("kbw"), tp[0].get("kbw")
                if not isinstance(sk, dict) or not isinstance(tk, dict):
                    raise KnowledgeError("only pages managed by kb-workbench can be merged automatically")
                for p, page_data in ((src, sp), (dst, tp)):
                    k = page_data[0]["kbw"]
                    if k.get("body_hash") and _body_hash(page_data[1]) != k["body_hash"]:
                        raise KnowledgeError(f"{p} was edited by hand; fold those edits in before merging")
                involved = {src: _version(sp[2]), dst: _version(tp[2])}
                merged, diff = self._merge_pages(tk, sk)
                plan = {"action": "merge", "from": src, "into": dst, "kept_title": tp[0].get("title"),
                        "dropped_title": sp[0].get("title"), "diff": diff,
                        "pages_with_links_to_fix": self._links_to(catalog, src),
                        "redirect": f"{src} and {sk['id']} -> {dst}"}
            token = sha({"plan": plan, "versions": involved})
            if not plan_token:
                return {"status": "preview", "plan": plan, "plan_token": token, "written": False,
                        "message": "review the plan; call again with plan_token to apply"}
            if plan_token != token:
                return {"status": "conflict", "written": False,
                        "message": "pages changed since the preview (or the request differs); preview again"}
            when = _now()
            if action == "move":
                k = sp[0].get("kbw")
                text = sp[2].decode("utf-8")
                if isinstance(k, dict):
                    k.setdefault("old_paths", []).append(src)
                    k.setdefault("saves", []).append({"at": when, "action": "移动", "note": f"{src} -> {dst}"})
                    self._write_page(dst, k, str(sp[0].get("title")), str(sp[0].get("created") or when), reg, catalog)
                else:
                    atomic_write(self._target(dst), self._rewrite_links(text, src, dst, {}))
                (self.wiki / src).unlink()
                moves = {src: dst}
                services = set(k.get("services") or []) if isinstance(k, dict) else set()
                fw = isinstance(k, dict) and k.get("kind") == "framework"
            else:
                merged["saves"].append({"at": when, "action": "合并", "note": f"merged {sk['id']} ({src})"})
                merged["aliases"] = list(dict.fromkeys((merged.get("aliases") or []) + [sk["id"]] + (sk.get("aliases") or [])))
                merged["old_paths"] = list(dict.fromkeys((merged.get("old_paths") or []) + [src] + (sk.get("old_paths") or [])))
                merged["related"] = [r for r in merged.get("related") or [] if r not in (sk["id"], tk["id"])]
                self._write_page(dst, merged, str(tp[0].get("title")), str(tp[0].get("created") or when), reg, catalog)
                (self.wiki / src).unlink()
                moves = {src: dst}
                reg["ids"][sk["id"]] = dst
                services = set(merged["services"]) | set(sk.get("services") or [])
                fw = "framework" in (merged.get("kind"), sk.get("kind"))
            for old, new in list(reg["moved"].items()):
                if new == src:
                    reg["moved"][old] = dst
            reg["moved"][src] = dst
            for pid, p in list(reg["ids"].items()):
                if p == src:
                    reg["ids"][pid] = dst
            self._save_registry(reg)
            fixed = self._fix_links(moves, merged_id=(sk["id"], tk["id"]) if action == "merge" else None, reg=reg)
            catalog = self.catalog()
            nav = self._refresh_navigation(catalog, services, fw, removed=[src] if action == "merge" else [],
                                           renamed={src: dst} if action == "move" else None)
            _append_log(self.wiki, "moved" if action == "move" else "merged", f"{src} -> {dst}",
                        f"kb-workbench organize_knowledge; links fixed in {len(fixed)} page(s)")
        index = self.reindex([dst] + fixed + nav)
        return {"status": "applied", "action": action, "from": src, "to": dst, "links_fixed": fixed,
                "navigation_updated": nav, "index": index, "searchable": index["status"] == "indexed"}

    def _merge_pages(self, target: dict, source: dict) -> tuple[dict, dict]:
        """Fold the source page's structured data into the target's (no model, exact
        de-duplication) and describe what would change, including likely conflicts."""
        new = {"kind": None, "title": "", "summary": "", "services": source.get("services") or [],
               "keywords": source.get("keywords") or [], "question": None, "tags": source.get("tags") or [],
               "project": source.get("project"), "refs": {},
               "evidence": [{k: v for k, v in e.items() if k != "id"} for e in source.get("evidence") or []],
               "inferences": [{"text": x["text"]} for x in source.get("inferences") or []],
               "open_questions": [{"text": x["text"]} for x in source.get("open_questions") or []]}
        idmap = {e["id"]: str(i) for i, e in enumerate(source.get("evidence") or [])}
        new["refs"] = {str(i): i for i in range(len(new["evidence"]))}
        for key in ("facts", "designs"):
            new[key] = [{"text": x["text"], "aspect": x.get("aspect", "fact"),
                         "evidence": [idmap[r] for r in x.get("evidence") or [] if r in idmap]}
                        for x in source.get(key) or []]
        merged = self._merge(target, new, [], [], False, _now())
        stats = merged.pop("_stats")
        merged["questions"] = list(dict.fromkeys((merged.get("questions") or []) + (source.get("questions") or [])))
        merged["history"] = (merged.get("history") or []) + [
            {**h, "id": f"h{len(merged.get('history') or []) + i + 1}"} for i, h in enumerate(source.get("history") or [])]
        merged["related"] = list(dict.fromkeys((merged.get("related") or []) + (source.get("related") or [])))
        if not target.get("summary") and source.get("summary"):
            merged["summary"] = source["summary"]
        conflicts = []
        for key in ("facts", "designs"):
            tgt = target.get(key) or []
            for s in source.get(key) or []:
                if any(_norm(s["text"]) == _norm(t["text"]) for t in tgt):
                    continue
                for t in tgt:
                    sim = _jaccard(_tokens(s["text"]), _tokens(t["text"]))
                    if sim >= 0.5:
                        conflicts.append({"section": key, "source": s["text"], "target": t["text"],
                                          "similarity": round(sim, 2),
                                          "hint": "similar but not identical: check whether they contradict"})
        kinds = {target.get("kind"), source.get("kind")}
        diff = {"added_from_source": stats["added"], "duplicates_collapsed": stats["duplicates_skipped"],
                "possible_conflicts": conflicts,
                "kind_mismatch": sorted(k for k in kinds if k) if len(kinds) > 1 else None,
                "summary_kept": target.get("summary")}
        return merged, diff

    def _links_to(self, catalog: list[dict], path: str) -> list[str]:
        out = []
        for c in catalog:
            for m in _LINK.finditer(c["body"]):
                if _resolve_link(m.group(2), c["path"]) == path:
                    out.append(c["path"])
                    break
        return out

    def _rewrite_links(self, text: str, old_page: str, new_page: str, moves: dict[str, str]) -> str:
        def sub(m):
            target = _resolve_link(m.group(2), old_page)
            if target is None:
                return m.group(0)
            target = moves.get(target, target)
            return m.group(1) + _rel(target, new_page) + m.group(3)
        return _LINK.sub(sub, text)

    def _fix_links(self, moves: dict[str, str], merged_id: tuple[str, str] | None, reg: dict) -> list[str]:
        """Point links (and related ids) at the new location in every page."""
        fixed = []
        catalog = self.catalog()
        for c in catalog:
            full = self.wiki / c["path"]
            k = c["fm"].get("kbw") if isinstance(c["fm"].get("kbw"), dict) else None
            if k is not None:
                rel_ids = k.get("related") or []
                changed = False
                if merged_id and merged_id[0] in rel_ids:
                    k["related"] = list(dict.fromkeys(merged_id[1] if r == merged_id[0] else r for r in rel_ids))
                    k["related"] = [r for r in k["related"] if r != k.get("id")]
                    changed = True
                if changed or any(m.group(2) and _resolve_link(m.group(2), c["path"]) in moves
                                  for m in _LINK.finditer(c["body"])):
                    if k.get("body_hash") and _body_hash(c["body"]) != k["body_hash"]:
                        continue  # hand-edited; check_knowledge will report the broken link
                    self._write_page(c["path"], k, c["title"], str(c["fm"].get("created") or _now()), reg, catalog)
                    fixed.append(c["path"])
                continue
            text = full.read_text(encoding="utf-8")
            new = self._rewrite_links(text, c["path"], c["path"], moves)
            if new != text:
                atomic_write(full, new)
                fixed.append(c["path"])
        return fixed

    # .............................................................. health check

    def check(self, fix: bool = False) -> dict:
        """Duplicate candidates, broken links, isolated pages, missing sources and
        pages whose sources changed. With fix=True only safe, mechanical repairs are
        applied: links to moved pages, index entries and hub pages."""
        catalog = self.catalog()
        reg = self._registry()
        paths = {c["path"] for c in catalog}
        managed = [c for c in catalog if isinstance(c["fm"].get("kbw"), dict)]
        dup = []
        for i, a in enumerate(managed):
            ka = a["fm"]["kbw"]
            res = self.find(a["title"], ka.get("services"), ka.get("keywords"),
                            [f"{e['repo']}:{e['path']}" for e in ka.get("evidence") or []], _catalog=catalog[:])
            for c in res["candidates"]:
                if c["path"] != a["path"] and c["strong"] and (c["path"], a["path"]) not in {(d["b"], d["a"]) for d in dup}:
                    dup.append({"a": a["path"], "b": c["path"], "score": c["score"], "reasons": c["reasons"],
                                "suggestion": "if same topic: organize_knowledge(action='merge', ...) (preview first)"})
        broken, inbound = [], {p: 0 for p in paths}
        for c in catalog:
            for m in _LINK.finditer(c["body"]):
                t = _resolve_link(m.group(2), c["path"])
                if t is None:
                    continue
                if t in paths:
                    if c["path"] not in NAV_FILES and not c["path"].startswith("hubs/"):
                        inbound[t] = inbound.get(t, 0) + 1
                else:
                    to, _ = self.resolve(t, reg)
                    broken.append({"page": c["path"], "link": m.group(2), "target": t,
                                   "redirect_to": to if to != t and to in paths else None})
        isolated = [p for p, n in inbound.items() if n == 0 and p not in NAV_FILES and not p.startswith(("hubs/", "sources/"))]
        missing, stale = [], []
        for c in catalog:
            fm = c["fm"]
            k = fm.get("kbw")
            if isinstance(k, dict):
                no_ev = [f["id"] for f in k.get("facts") or [] if not f.get("evidence")]
                if not k.get("evidence") or no_ev:
                    missing.append({"page": c["path"], "reason": "no evidence" if not k.get("evidence")
                                    else f"facts without evidence: {no_ev}"})
                sc = self.source_check(k)
                if sc["summary"] == "needs_review":
                    stale.append({"page": c["path"], "page_id": k.get("id"),
                                  "evidence": [e for e in sc["evidence"] if e["status"] != "unchanged"
                                               or e.get("uncommitted_changes_now")]})
            elif not c["path"].startswith(("hubs/", "sources/")) and c["path"] not in NAV_FILES \
                    and not fm.get("sources") and not fm.get("source_path"):
                missing.append({"page": c["path"], "reason": "page declares no sources (not managed by kb-workbench)"})
        idx = {e["path"] for e in _read_index(self.wiki / "index.md")}
        unindexed = [c["path"] for c in managed if c["path"] not in idx]
        result = {"duplicates": dup, "broken_links": broken, "isolated": isolated, "missing_sources": missing,
                  "possibly_stale": stale, "not_in_index": unindexed, "pending_reindex": self.pending(),
                  "note": "Source changes are review signals, not proof that a page is outdated. "
                          "Merges and deletions are never done here; use organize_knowledge (preview first)."}
        if fix:
            self._require_writable()
            with self._lock().hold():
                moves = {b["target"]: b["redirect_to"] for b in broken if b["redirect_to"]}
                fixed = self._fix_links(moves, None, reg) if moves else []
                catalog = self.catalog()
                services = {s for c in managed for s in c["fm"]["kbw"].get("services") or []}
                nav = self._refresh_navigation(catalog, services, any(c["fm"]["kbw"].get("kind") == "framework"
                                                                       for c in managed))
            result["fixed"] = {"links": fixed, "navigation": nav, "index": self.reindex(fixed + nav)}
        return result


@contextmanager
def _nullcontext():
    yield

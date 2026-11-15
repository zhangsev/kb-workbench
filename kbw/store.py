"""SQLite store: knowledge chunks, FTS5, optional sqlite-vec vectors, exact cache."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .util import fts_query, fts_tokens, log

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(page TEXT PRIMARY KEY, sig TEXT, frontmatter TEXT);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, page TEXT NOT NULL, title TEXT, heading TEXT,
  start_line INTEGER, end_line INTEGER, text TEXT);
CREATE INDEX IF NOT EXISTS chunks_page ON chunks(page);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(tok, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT, created REAL);
CREATE TABLE IF NOT EXISTS repo_index(name TEXT PRIMARY KEY, indexed_commit TEXT, indexed_at REAL, tool_version TEXT);
"""


class Store:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "kbw.sqlite"
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.vec_ok, self.vec_error = self._load_vec()

    def _load_vec(self) -> tuple[bool, str | None]:
        try:
            import sqlite_vec
        except ImportError:
            return False, "python package sqlite-vec is not installed"
        try:
            self.db.enable_load_extension(True)
            sqlite_vec.load(self.db)
            self.db.enable_load_extension(False)
            return True, None
        except (AttributeError, sqlite3.Error) as exc:
            return False, f"cannot load the sqlite-vec extension: {exc}"

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ------------------------------------------------------------ meta

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    # ------------------------------------------------------------ vectors

    def ensure_vec_table(self, dim: int, model: str) -> None:
        """(Re)create the vector table when the embedding model or size changed."""
        with self._lock, self.db:
            current = self.get_meta("vec")
            if current != {"dim": dim, "model": model}:
                self.db.execute("DROP TABLE IF EXISTS chunks_vec")
                self.db.execute(f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{int(dim)}])")
                self.db.execute("INSERT OR REPLACE INTO meta VALUES('vec',?)",
                                (json.dumps({"dim": dim, "model": model}),))
                self.db.execute("UPDATE files SET sig=''")  # force re-embedding of every page

    def has_vectors(self) -> bool:
        if not self.vec_ok or not self.get_meta("vec"):
            return False
        with self._lock:
            return self.db.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] > 0

    # ------------------------------------------------------------ pages

    def file_sigs(self) -> dict[str, str]:
        with self._lock:
            return {r[0]: r[1] for r in self.db.execute("SELECT page, sig FROM files")}

    def delete_page(self, page: str) -> None:
        with self._lock, self.db:
            ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE page=?", (page,))]
            for cid in ids:
                self.db.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
                if self.vec_ok and self.get_meta("vec"):
                    self.db.execute("DELETE FROM chunks_vec WHERE rowid=?", (cid,))
            self.db.execute("DELETE FROM chunks WHERE page=?", (page,))
            self.db.execute("DELETE FROM files WHERE page=?", (page,))

    def add_page(self, page: str, sig: str, frontmatter: dict, chunks: list[dict],
                 vectors: list | None) -> None:
        self.delete_page(page)
        with self._lock, self.db:
            self.db.execute("INSERT INTO files VALUES(?,?,?)",
                            (page, sig, json.dumps(frontmatter, ensure_ascii=False, default=str)))
            for i, c in enumerate(chunks):
                cur = self.db.execute(
                    "INSERT INTO chunks(page,title,heading,start_line,end_line,text) VALUES(?,?,?,?,?,?)",
                    (page, c["title"], c["heading"], c["start_line"], c["end_line"], c["text"]))
                cid = cur.lastrowid
                tok = " ".join(fts_tokens(f'{c["title"]} {c["heading"]} {c["text"]}'))
                self.db.execute("INSERT INTO chunks_fts(rowid, tok) VALUES(?,?)", (cid, tok))
                if vectors is not None:
                    import sqlite_vec
                    self.db.execute("INSERT INTO chunks_vec(rowid, embedding) VALUES(?,?)",
                                    (cid, sqlite_vec.serialize_float32(list(map(float, vectors[i])))))

    def frontmatter(self, page: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT frontmatter FROM files WHERE page=?", (page,)).fetchone()
        return json.loads(row[0]) if row else None

    def counts(self) -> dict:
        with self._lock:
            pages = self.db.execute("SELECT count(*) FROM files").fetchone()[0]
            chunks = self.db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        return {"pages": pages, "chunks": chunks, "vectors": self.has_vectors()}

    # ------------------------------------------------------------ search

    def _rows(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        with self._lock:
            got = {r["id"]: dict(r) for r in self.db.execute(
                f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(ids))})", ids)}
        return [got[i] for i in ids if i in got]

    def search_fts(self, query: str, limit: int = 30) -> list[dict]:
        q = fts_query(query)
        if not q:
            return []
        try:
            with self._lock:
                ids = [r[0] for r in self.db.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                    (q, limit))]
        except sqlite3.Error as exc:
            log.warning("fts query failed: %s", exc)
            return []
        return self._rows(ids)

    def search_vec(self, vector, limit: int = 30) -> list[dict]:
        import sqlite_vec
        with self._lock:
            ids = [r[0] for r in self.db.execute(
                "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (sqlite_vec.serialize_float32(list(map(float, vector))), limit))]
        return self._rows(ids)

    def best_chunk(self, page: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM chunks WHERE page=? ORDER BY id LIMIT 1", (page,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ cache

    def cache_get(self, key: str):
        with self._lock:
            row = self.db.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, key: str, value, keep: int = 500) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                            (key, json.dumps(value, ensure_ascii=False, default=str), time.time()))
            self.db.execute("DELETE FROM cache WHERE key NOT IN "
                            "(SELECT key FROM cache ORDER BY created DESC LIMIT ?)", (keep,))

    def cache_size(self) -> int:
        with self._lock:
            return self.db.execute("SELECT count(*) FROM cache").fetchone()[0]

    # ------------------------------------------------------------ repos

    def set_repo_index(self, name: str, commit: str | None, tool_version: str | None) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO repo_index VALUES(?,?,?,?)",
                            (name, commit, time.time(), tool_version))

    def repo_index(self, name: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM repo_index WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

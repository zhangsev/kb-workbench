"""Local embedding and reranking through FastEmbed (CPU ONNX Runtime).

Models are loaded lazily. When a model or the library is missing the caller gets
a clear `status` and retrieval degrades to full-text / exact search. There is no
hash-vector fallback: no model means no semantic route.
"""
from __future__ import annotations

import threading
from pathlib import Path

from .util import log

# Instruction recommended by the model authors for short query -> passage retrieval.
BGE_ZH_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class _Lazy:
    def __init__(self, name: str, model_dir: Path, offline: bool, enabled: bool = True):
        self.name, self.model_dir, self.offline, self.enabled = name, model_dir, offline, enabled
        self._model = None
        self._error: str | None = None if enabled else "disabled in config"
        self._lock = threading.Lock()

    def _build(self):
        raise NotImplementedError

    def model_present(self) -> bool:
        """True when files for this model are already in the model directory."""
        tail = self.name.split("/")[-1].lower()
        if not self.model_dir.is_dir():
            return False
        return any(tail in p.name.lower() for p in self.model_dir.iterdir())

    def load(self, allow_download: bool = False):
        with self._lock:
            if self._model is not None or (self._error and not allow_download):
                return self._model
            if not self.enabled:
                return None
            if not allow_download and not self.model_present():
                # Never download implicitly: only `kbw setup` may fetch models.
                self._error = (f"model files for {self.name} not found in {self.model_dir}; "
                               "run `kbw setup` (online) or copy the model directory (offline)")
                return None
            try:
                self._model = self._build(local_only=not allow_download or self.offline)
                self._error = None
            except ImportError:
                self._error = "python package fastembed is not installed"
            except Exception as exc:  # model load failures must never break search
                self._error = f"cannot load {self.name}: {type(exc).__name__}: {exc}"
                log.warning(self._error)
            return self._model

    @property
    def status(self) -> dict:
        state = "loaded" if self._model is not None else ("unavailable" if self._error else "not_loaded")
        return {"model": self.name, "state": state, "reason": self._error}


class Embedder(_Lazy):
    def _build(self, local_only: bool):
        from fastembed import TextEmbedding
        return TextEmbedding(model_name=self.name, cache_dir=str(self.model_dir), local_files_only=local_only)

    def embed_passages(self, texts: list[str]) -> list | None:
        model = self.load()
        return [v for v in model.embed(texts)] if model is not None else None

    def embed_query(self, text: str):
        model = self.load()
        if model is None:
            return None
        prefix = BGE_ZH_QUERY_PREFIX if "bge" in self.name.lower() and "zh" in self.name.lower() else ""
        return next(iter(model.embed([prefix + text])))


class Reranker(_Lazy):
    def _build(self, local_only: bool):
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        return TextCrossEncoder(model_name=self.name, cache_dir=str(self.model_dir), local_files_only=local_only)

    def scores(self, query: str, docs: list[str]) -> list[float] | None:
        model = self.load()
        return [float(s) for s in model.rerank(query, docs)] if model is not None else None

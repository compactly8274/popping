"""Embedding pipeline.

Wraps sentence-transformers with:
  - Lazy model load (slow first call; fast subsequent calls).
  - asyncio-friendly API (CPU-bound work runs in a thread pool).
  - Graceful failure mode — embed() can return None / a zero vector if
    the model isn't loaded, instead of crashing the ingest path.

The model is loaded once on backend startup (see app.main lifespan) so
the first ingest doesn't pay the ~3s import cost. The model itself is
the canonical all-MiniLM-L6-v2 from HuggingFace — 384-dim output,
~80 MB on disk, runs on CPU.

If ``EMBEDDING_ENABLED=false`` is set in env, ``embed()`` returns a
zero vector and ``embed_many()`` returns a list of zero vectors.
This is the safe fallback for memory-constrained hosts.
"""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app.config import settings

logger = logging.getLogger("popping.embeddings")

# Worker count for the embedding thread pool. One worker serialized
# every per-entry embed (and the backfill job) through a single
# thread — a busy backfill starved live ingest. Multiple workers let
# several sentence-transformers encodes run in parallel. ``encode``
# releases the GIL via numpy, so this scales almost linearly up to
# the CPU count. Falls back to 2 on hosts where ``cpu_count`` is
# unset (cgroup-limited containers).
_DEFAULT_WORKERS = max(os.cpu_count() or 2, 2)

# Singleton — module-level so the scheduler and the foryou endpoint
# share the loaded model.
_embedder: "Embedder | None" = None


def embedder() -> "Embedder":
    """Module-level accessor. Returns the singleton; constructs it lazily
    so importing this module is cheap."""
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


class Embedder:
    """sentence-transformers wrapper.

    Loads on first ``load()`` call (or on first ``embed()`` call if
    ``load()`` was never explicitly called). Thread-safe via the GIL —
    sentence-transformers' ``encode`` is released back to the loop only
    when we await the executor.
    """

    def __init__(self) -> None:
        self._model = None
        self._executor: ThreadPoolExecutor | None = None
        self._dim: int = 384
        self._loaded = False

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def loaded(self) -> bool:
        return self._loaded

    async def load(self) -> None:
        """Load the model. Safe to call repeatedly."""
        if self._loaded:
            return
        if not settings.embedding_enabled:
            logger.info("embeddings disabled (EMBEDDING_ENABLED=false); skipping load")
            self._loaded = True  # mark so we don't try again
            return
        loop = asyncio.get_running_loop()
        # Loading is CPU + disk bound; run in the default executor.
        # The actual sentence_transformers import lives inside _load_sync
        # so the binding is in scope on the worker thread.
        await loop.run_in_executor(None, self._load_sync, settings.embedding_model)
        # Multi-worker pool — was 1, which serialized every embedding
        # call. With one worker the periodic backfill job (50k rows)
        # queued up calls that blocked live ingest behind them.
        self._executor = ThreadPoolExecutor(
            max_workers=_DEFAULT_WORKERS, thread_name_prefix="embed"
        )
        self._loaded = True
        logger.info("embedding model loaded: %s (dim=%d)", settings.embedding_model, self._dim)

    def _load_sync(self, model_name: str) -> None:
        # Import inside the worker so the binding is in scope (executors
        # run on separate threads — a name bound in ``load()`` isn't
        # visible here). Also keeps the heavy torch+transformers import
        # off the import path.
        from sentence_transformers import SentenceTransformer

        # ``device='cpu'`` is the explicit default but pinned here so a
        # GPU-enabled torch install doesn't try to claim VRAM we don't have.
        self._model = SentenceTransformer(model_name, device="cpu")
        self._dim = self._model.get_sentence_embedding_dimension()

    async def embed(self, text: str) -> Optional[list[float]]:
        """Embed one string. Returns None if embeddings are disabled or
        the model isn't loaded yet. Returns a 384-dim list otherwise."""
        if not settings.embedding_enabled or not self._loaded or self._model is None:
            return None
        text = (text or "").strip()
        if not text:
            return [0.0] * self._dim
        loop = asyncio.get_running_loop()
        vec = await loop.run_in_executor(self._executor, self._embed_one, text)
        return vec

    def _embed_one(self, text: str) -> list[float]:
        result = self._model.encode([text], convert_to_numpy=True, show_progress_bar=False)
        return result[0].tolist()

    async def embed_many(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Embed a batch. Returns one vector per input, in order. None
        entries appear where the text was empty."""
        if not settings.embedding_enabled or not self._loaded or self._model is None:
            return [None] * len(texts)
        # Filter empties but remember positions so the output order matches.
        positions = [(i, t) for i, t in enumerate(texts) if (t or "").strip()]
        if not positions:
            return [[0.0] * self._dim for _ in texts]
        indices, clean = zip(*positions)
        loop = asyncio.get_running_loop()
        vectors = await loop.run_in_executor(self._executor, self._embed_many_sync, list(clean))
        out: list[Optional[list[float]]] = [None] * len(texts)
        for idx, vec in zip(indices, vectors):
            out[idx] = vec
        # Fill empty slots with zero vectors (keeps downstream happy).
        for i, t in enumerate(texts):
            if out[i] is None:
                out[i] = [0.0] * self._dim
        return out

    def _embed_many_sync(self, texts: list[str]) -> list[list[float]]:
        result = self._model.encode(
            texts,
            batch_size=settings.embedding_batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in result]
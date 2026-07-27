"""Personal scoring: vector similarity + category preference.

Combines two signals into a single ``personal_score`` in [0, 100]:

  - cosine similarity between ``entry.embedding`` and the user's
    ``preference_vector``, rescaled from [-1, 1] to [0, 100].
  - multiplicative adjustment for category: followed categories get a
    1.2× boost, muted categories a 0.5× dampen. Both are JSON arrays
    of category names on the user profile.

NULL embeddings or NULL preference_vector return a neutral 50 (the
"no signal yet" midpoint) before the category adjustment. This keeps
the dashboard usable during cold start instead of collapsing the feed
to zero.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from app.models import Entry, Source, UserProfile

NEUTRAL = 50.0
FOLLOW_BOOST = 1.2
MUTE_DAMP = 0.5


def _cosine(a, b) -> Optional[float]:
    """Cosine similarity in [-1, 1]. Returns None if either input is
    missing or lengths don't match.

    ``a`` / ``b`` may arrive as a Python list (``embedding`` column
    when set in-process), a ``numpy.ndarray`` (when read back from
    the Postgres ``vector`` column via the pgvector dialect), or
    ``None``. Coerce to ``np.asarray(..., dtype=float)`` up front so
    the body uses a single vectorized path — slice 16 added numpy to
    the rescore path's aggregate step but missed this per-call site.

    Returns ``None`` (not a raise, not 0.0) for the documented edge
    cases — missing input, mismatched lengths, or zero-norm — so
    callers can map ``None`` → the neutral midpoint (``vector_score``
    below). The previous Python for-loop kept this contract; the
    numpy rewrite preserves it.
    """
    if a is None or b is None:
        return None
    # ``np.asarray`` is a no-op for already-ndarray inputs (returns
    # the same object) and a cheap copy for list inputs. ``dtype=float``
    # avoids integer-array behaviour on a pgvector ``bigint``-encoded
    # read-back (rare, but defensive — the prior Python loop did this
    # implicitly via ``x * y``).
    a_arr = np.asarray(a, dtype=float)
    b_arr = np.asarray(b, dtype=float)
    if a_arr.size == 0 or b_arr.size == 0 or a_arr.shape != b_arr.shape:
        return None
    dot = float(np.dot(a_arr, b_arr))
    na = float(np.dot(a_arr, a_arr))
    nb = float(np.dot(b_arr, b_arr))
    if na == 0.0 or nb == 0.0:
        return None
    # ``math.sqrt`` is fine on a Python float — wrapping np.sqrt would
    # add overhead for a single-element reduction.
    return dot / (math.sqrt(na) * math.sqrt(nb))


def vector_score(entry_emb, pref_vec) -> float:
    """Map cosine similarity → 0..100. None inputs return the neutral 50.

    Both arguments can be a list, a numpy.ndarray (pgvector read-back),
    or None — ``_cosine`` normalises either.

    Public (not just this module's ``score()``): ``app.feed_recommendations``
    reuses this to rank the curated feed list by similarity to the same
    ``preference_vector`` that drives For You, rather than duplicating
    the cosine + rescale + neutral-fallback logic.
    """
    c = _cosine(entry_emb, pref_vec)
    if c is None:
        return NEUTRAL
    # Rescale [-1, 1] → [0, 100]. 0.5 cosine = 75, perfect = 100, opposite = 0.
    return max(0.0, min(100.0, 50.0 + 50.0 * c))


def _category_multiplier(
    category: Optional[str],
    followed: Optional[list],
    muted: Optional[list],
) -> float:
    cat = (category or "").lower().strip()
    if not cat:
        return 1.0
    if followed and cat in {str(c).lower().strip() for c in followed}:
        return FOLLOW_BOOST
    if muted and cat in {str(c).lower().strip() for c in muted}:
        return MUTE_DAMP
    return 1.0


def score(entry: Entry, source: Source | None, profile: UserProfile | None) -> float:
    """Personal score for an entry given its source's category and the
    user's profile. Returns a float in roughly [0, 100].

    ``entry`` is typed as ``Entry`` for documentation but in practice
    can be any object exposing ``embedding`` — a real ORM row, the
    slim ``_Row`` shim built by ``/api/foryou``, etc. Use ``getattr``
    with ``None`` so callers that explicitly exclude ``embedding``
    from their SELECT (the For You slim projection does, to keep
    ~3KB of vector data off every candidate row) still get the
    documented "NULL embedding → neutral 50" behaviour instead of
    an ``AttributeError`` that 500s the whole endpoint.

    Delegates to ``score_partial``; kept for callers that have a
    real ``Entry`` + ``Source`` + ``UserProfile`` (the rescore loop
    at read-time). The ingest path uses ``score_partial`` directly
    because at ingest time there IS no Entry yet — only the
    embedding vector (from the embedder) and the source / profile
    (already loaded)."""
    embedding = getattr(entry, "embedding", None)
    return score_partial(
        embedding=embedding,
        source_category=source.category if source else None,
        preference_vector=profile.preference_vector if profile else None,
        followed_categories=profile.followed_categories if profile else None,
        muted_categories=profile.muted_categories if profile else None,
    )


def score_partial(
    *,
    embedding: Optional[list[float]],
    source_category: Optional[str],
    preference_vector: Optional[list[float]],
    followed_categories: Optional[list],
    muted_categories: Optional[list],
) -> float:
    """Personal score from individual fields (no Entry / Source /
    UserProfile required).

    Same semantics as ``score()``: NULL embedding or NULL
    preference_vector return a neutral 50 before the category
    adjustment, the result is clamped to [0, 120] and rounded to
    1 decimal.

    Keyword-only signature so the 5 args are unambiguous at every
    call site; the ingest path reads better as
    ``composite.score_partial(embedding=..., source_category=..., ...)``
    than as a positional bag of values where argument 4 might be
    ``followed_categories`` or ``muted_categories`` depending on
    caller whim.
    """
    vec = vector_score(embedding, preference_vector)
    cat_mult = _category_multiplier(
        source_category,
        followed_categories,
        muted_categories,
    )
    out = vec * cat_mult
    # Clamp — a heavy mute can drag below 0, but downstream expects
    # a comparable magnitude to recency (0-100). Round to 1dp.
    return max(0.0, min(120.0, round(out, 1)))

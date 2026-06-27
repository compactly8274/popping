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

from app.models import Entry, Source, UserProfile

NEUTRAL = 50.0
FOLLOW_BOOST = 1.2
MUTE_DAMP = 0.5


def _cosine(a: Optional[list[float]], b: Optional[list[float]]) -> Optional[float]:
    """Cosine similarity in [-1, 1]. Returns None if either input is
    missing or lengths don't match."""
    if not a or not b or len(a) != len(b):
        return None
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return None
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _vector_score(entry_emb: Optional[list[float]], pref_vec: Optional[list[float]]) -> float:
    """Map cosine similarity → 0..100. None inputs return the neutral 50."""
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
    user's profile. Returns a float in roughly [0, 100]."""
    vec = _vector_score(entry.embedding, profile.preference_vector if profile else None)
    cat_mult = _category_multiplier(
        source.category if source else None,
        profile.followed_categories if profile else None,
        profile.muted_categories if profile else None,
    )
    out = vec * cat_mult
    # Clamp — a heavy mute can drag below 0, but downstream expects
    # a comparable magnitude to recency (0-100). Round to 1dp.
    return max(0.0, min(120.0, round(out, 1)))

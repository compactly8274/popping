"""Engagement scoring — votes, comments, and reactions.

Different sources ship different engagement signals:

    HN         ``score`` (points) + ``descendants`` (comments)
    Reddit     ``score`` (upvotes − downvotes) + ``num_comments``
    RFD        vote / reply / view counts (forum RSS exposes them
               inconsistently across feed versions)
    GitHub     ``stargazers_count`` / ``forks_count`` / comments
    YouTube    ``view_count`` / ``like_count``

The composite scorer needs ONE signal: how much interaction is
this item getting, on a 0-100 scale, normalized so a high-engagement
item from a small community doesn't overwhelm an OK item from a
popular one. We centralize the math here and let each source
populate the canonical ``engagement_*`` meta keys.

Two-tier metadata scheme:
    1. Source-specific meta keys live in ``Entry.meta`` under their
       natural names (``score``, ``comments``, ``votes``, ``view_count``).
       Sources write them and existing UI/queries that look at
       ``meta.score`` keep working.
    2. Canonical engagement keys (``engagement_score``,
       ``engagement_comments``) are what THIS module reads. Sources
       that don't ship engagement signals simply don't set them;
       ``score()`` returns 0 and the composite formula gives them
       no engagement boost.

Why canonical keys rather than reading from per-source names? The
list of keys keeps growing as new sources ship. A central read
site is the right place to map them, and source plugins only need
to write the canonical names they care about. Existing per-source
keys (HN's ``meta.score``) stay for backwards compat with the
schema / frontend.

The math:
    - log10(1+n) compresses the long tail so 10000 votes isn't 100×
      more engaging than 100 votes. Reddit's "hot" ranking uses the
      same log scale.
    - tanh() keeps the result in [0, 1] before we rescale to 100. A
      purely linear combination of log terms can run away when both
      votes AND comments are very high.
    - Comments weighted higher than votes. A comment takes more time
      and signals deeper interest than an upvote, so it should
      contribute more to the "this is hot right now" reading.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from app.models import Entry

# Component weights on the log-scale inputs. Tunable; comments cost
# the reader more time than a vote, so we weight them slightly higher.
# These are NOT the final blend weights — composite.py multiplies by
# ``scoring_weight_engagement``. These are relative weights inside
# this module's formula.
_VOTE_WEIGHT = 0.6
_COMMENT_WEIGHT = 0.9


def _safe_log1p(n: Optional[int | float]) -> float:
    """log10(1 + max(n, 0)). Negative / None inputs return 0."""
    if n is None:
        return 0.0
    try:
        n = float(n)
    except (TypeError, ValueError):
        return 0.0
    if n <= 0 or math.isnan(n) or math.isinf(n):
        return 0.0
    return math.log10(1.0 + n)


def _read_meta(meta: Any, *keys: str) -> Optional[float]:
    """First non-None numeric value among ``keys`` in ``meta`` (dict
    or None). Strings that parse as floats are coerced; everything
    else returns None so we don't trip on RFD's "1.2k" patterns
    silently. Returns None when no key yields a usable number."""
    if not meta or not isinstance(meta, dict):
        return None
    for key in keys:
        val = meta.get(key)
        if val is None:
            continue
        if isinstance(val, bool):
            # bool is a subclass of int; treat True as 1.0, False as 0.0
            val = 1.0 if val else 0.0
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            stripped = val.strip()
            if not stripped:
                continue
            try:
                return float(stripped)
            except ValueError:
                continue
    return None


def score(entry: Entry, source: Any = None) -> float:
    """Engagement score for an entry in [0, 100].

    Reads canonical meta keys first, then falls back to legacy
    per-source names so already-ingested rows score correctly
    without a backfill:

        canonical           legacy fallback
        engagement_score    score, votes, upvotes, points, view_count
        engagement_comments comments, replies, descendants, num_comments

    Returns 0 when no engagement signals are present (BBC, NVD,
    CISA, Wikipedia — none ship engagement today). That's a
    correct "we have no signal" answer, not a bug; the composite
    formula gives them no engagement boost, which is what we want.
    """
    meta = entry.meta if entry.meta else {}

    votes = _read_meta(
        meta,
        "engagement_score",
        "score",
        "votes",
        "upvotes",
        "points",
    )
    comments = _read_meta(
        meta,
        "engagement_comments",
        "comments",
        "replies",
        "descendants",
        "num_comments",
    )

    # If a source ships one signal but not the other, treat the
    # missing one as zero — not None — so the formula still runs.
    log_votes = _VOTE_WEIGHT * _safe_log1p(votes)
    log_comments = _COMMENT_WEIGHT * _safe_log1p(comments)

    # Both zero → zero engagement. Short-circuit so we don't bother
    # with tanh on a no-signal entry.
    if log_votes == 0.0 and log_comments == 0.0:
        return 0.0

    # tanh saturates around log10 value ~3.3 (i.e. ~2000 votes + 2000
    # comments at the default weights). That's the right behavior —
    # past a few thousand votes/comments everything is "very hot".
    raw = log_votes + log_comments
    bounded = math.tanh(raw)
    return round(100.0 * bounded, 1)
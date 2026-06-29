"""Engagement scoring — votes, comments, and reactions.

Different sources ship different engagement signals:

    HN         ``score`` (points) + ``descendants`` (comments)
    Reddit     ``score`` (upvotes − downvotes) + ``num_comments``
    RFD        vote / reply / view counts (forum RSS exposes them
               inconsistently across feed versions)
    GitHub     ``stargazers_count`` / ``forks_count`` / comments
    YouTube    ``like_count`` (view_count is audience-size, NOT
               treated as engagement here)

The composite scorer needs ONE signal: how much interaction is
this item getting, on a 0-100 scale, normalized so a high-engagement
item from a small community doesn't overwhelm an OK item from a
popular one. We centralize the math here and let each source
populate the canonical ``engagement_*`` meta keys.

Two-tier metadata scheme:
    1. Source-specific meta keys live in ``Entry.meta`` under their
       natural names (``score``, ``comments``, ``votes``, ``stars``,
       ``likes``, ``reactions``, ``bookmarks``, ``shares``, …).
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

What we DO treat as engagement:
    - ``stars`` / ``star_count`` — GitHub stars, podcast ratings
    - ``reactions`` / ``reactions_count`` — Facebook / Slack /
      Reddit-style aggregates
    - ``likes`` — explicit thumbs-up
    - ``bookmarks`` — saved-for-later (Pocket, HN favorite)
    - ``shares`` — explicit share events
    - ``replies`` / ``replies_count`` — discussion-shaped comments

What we DO NOT treat as engagement (deliberately excluded):
    - ``view_count`` / ``plays`` / ``listens`` — audience size,
      not per-item interest. A 1M-view YouTube video isn't "more
      engaged" than a 200-comment HN thread.
    - ``clicks`` — outbound CTR is a marketing-funnel metric, not
      content-engagement. Mixing it would dilute the votes signal.

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


# Lookup tables for the fallback chain in ``score()``. Tuples (not sets)
# because order matters: we return the FIRST numeric value we find.
# Sources write their natural key — ``points``, ``stars``, ``likes``,
# whatever — and we look it up here.
#
# Adding a new key is a deliberate act: it has to be unambiguously
# engagement-shaped and not a "this looks large enough to count"
# heuristic. See the module docstring's "What we DO treat as
# engagement" / "What we DO NOT" lists for the inclusion criteria.
#
# Canonical ``engagement_score`` / ``engagement_comments`` always come
# first so a source that writes both gets canonical semantics.
ENGAGEMENT_VOTE_KEYS: tuple[str, ...] = (
    "engagement_score",
    "score",
    "votes",
    "upvotes",
    "points",
    "stars",
    "star_count",
    "reactions",
    "reactions_count",
    "likes",
    "bookmarks",
    "shares",
)
ENGAGEMENT_COMMENT_KEYS: tuple[str, ...] = (
    "engagement_comments",
    "comments",
    "replies",
    "replies_count",
    "descendants",
    "num_comments",
)


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

    Reads canonical meta keys first, then falls back to a wide
    per-source-name list (see ``ENGAGEMENT_VOTE_KEYS`` and
    ``ENGAGEMENT_COMMENT_KEYS``) so already-ingested rows score
    correctly without a backfill and so a new source plugin that
    writes ``meta.stars`` / ``meta.likes`` / ``meta.reactions``
    lights up engagement scoring without code changes here.

    Returns 0 when no engagement signals are present (BBC, NVD,
    CISA, Wikipedia — none ship engagement today). That's a
    correct "we have no signal" answer, not a bug; the composite
    formula gives them no engagement boost, which is what we want.

    ``meta`` is optional on ``entry`` — list endpoints (notably
    ``/api/foryou``) build slim projections that exclude the JSONB
    blob to save ~500 B / row. ``getattr(..., None)`` returns a
    "no engagement" answer for those rows (the For You slim path
    is read-side; engagement has already been folded into
    ``composite_score`` at ingest, which is what the dashboard
    actually ranks on)."""
    meta = getattr(entry, "meta", None) or {}

    votes = _read_meta(meta, *ENGAGEMENT_VOTE_KEYS)
    comments = _read_meta(meta, *ENGAGEMENT_COMMENT_KEYS)

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
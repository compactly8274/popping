"""Composite scoring: blends recency, personal, source weight, and engagement.

The final score a card sorts by. Weights come from Settings so they
can be tuned without a code change:

    composite = w_r * recency
              + w_p * personal
              + w_s * (raw_score * source_weight)
              + w_e * engagement

``raw_score`` (an existing column) is the recency score at ingest time,
so newly-ingested entries start high and decay. The convergence boost
(applied at query time, not here) multiplies composite for items that
appear in multiple sources within the window.

Engagement is the new fourth component. Sources that ship votes /
comments / replies (HN, RFD, Reddit, GitHub) populate ``Entry.meta``
with the canonical ``engagement_score`` / ``engagement_comments``
keys (or the legacy ``score`` / ``comments`` names) and this
component lifts them. Sources without engagement signals
(BBC, NVD, CISA, Wikipedia) contribute zero — re-weighting the
formula doesn't move them. See ``app.scoring.engagement`` for the
per-source mapping and the log-tanh curve.
"""

from __future__ import annotations

import datetime as dt

from app.config import settings
from app.models import Entry, Source, UserProfile
from app.scoring import engagement, personal, recency, source as source_helper


def score(
    entry: Entry,
    source: Source | None,
    profile: UserProfile | None,
    now: dt.datetime | None = None,
) -> float:
    """Compute composite_score for one entry."""
    r = recency.score(entry.published_at, source.category if source else None, now=now)
    p = personal.score(entry, source, profile)
    sw = source_helper.weight(source)
    # raw_score is the recency-at-ingest stored value; source_weight tilts
    # entire sources up or down. Both default to neutral.
    raw = float(entry.raw_score or 0.0)
    s = raw * sw
    e = engagement.score(entry, source)
    total = (
        settings.scoring_weight_recency * r
        + settings.scoring_weight_personal * p
        + settings.scoring_weight_source * s
        + settings.scoring_weight_engagement * e
    )
    return round(total, 2)


def title_slug(title: str | None, n_words: int = 8) -> str:
    """Normalize a title for convergence comparison. Lowercase, strip
    punctuation, collapse whitespace, take the first n words. Two BBC
    rewrites of the same story land on the same slug."""
    import re

    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    words = t.split()[:n_words]
    return " ".join(words)


def convergence_multiplier(source_count: int) -> float:
    """Multiplicative boost for cross-source story clusters.

    1 source → 1.0 (no boost)
    2 sources → settings.convergence_boost_2
    3+ sources → settings.convergence_boost_3plus
    """
    if source_count >= 3:
        return settings.convergence_boost_3plus
    if source_count == 2:
        return settings.convergence_boost_2
    return 1.0

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
    published_at,
    raw_score: float,
    embedding,
    meta: dict | None,
    source: Source | None,
    profile: UserProfile | None,
    now: dt.datetime | None = None,
) -> float:
    """Compute composite_score from the fields the formula actually reads.

    Fields, not an ``Entry`` ORM row, so the ingest hot path can
    call this between INSERT and commit without fabricating a
    transient Entry (see ``scheduler._stub_entry`` — this refactor
    makes that workaround unnecessary). The wrapper below
    preserves the old ``(entry, source, profile)`` signature for
    the rescore hot path and the ``/api/foryou`` route, which
    pass real ORM rows.
    """
    r = recency.score(published_at, source.category if source else None, now=now)
    p = personal.score(embedding, source, profile)
    sw = source_helper.weight(source)
    raw = float(raw_score or 0.0)
    s = raw * sw
    e = engagement.score_from_meta(meta, source)
    total = (
        settings.scoring_weight_recency * r
        + settings.scoring_weight_personal * p
        + settings.scoring_weight_source * s
        + settings.scoring_weight_engagement * e
    )
    return round(total, 2)


def score_entry(
    entry: Entry,
    source: Source | None,
    profile: UserProfile | None,
    now: dt.datetime | None = None,
) -> float:
    """Backwards-compatible wrapper for callers that pass a real Entry
    (the rescore hot path, the ``/api/foryou`` route). Just unwraps
    the fields the formula needs and delegates to ``score()``.

    Kept as a separate function (not a default-argument shim) so the
    call site is explicit and the field-passing path is the one
    that shows up in profilers / type hints.
    """
    return score(
        published_at=entry.published_at,
        raw_score=entry.raw_score or 0.0,
        embedding=getattr(entry, "embedding", None),
        meta=getattr(entry, "meta", None),
        source=source,
        profile=profile,
        now=now,
    )


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

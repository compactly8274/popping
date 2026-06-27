"""Recency scoring with per-category half-life.

Returns a score in [0, 100]. 100 = just published; 0 = several half-lives
old. Phase 2 uses a half-life per category so different verticals age at
different rates:

  - news/vulns      6h    (freshness matters; old news is stale)
  - deals           48h   (deal posts are evergreen — they don't age out)
  - sports          3h    (results have very short relevance)
  - tech            12h   (HN, GitHub releases — fast refresh keeps them current)
  - everything else 12h   (sensible default)
"""

from __future__ import annotations

import datetime as dt
import math

_HALF_LIFE_HOURS = {
    "news": 6.0,
    "vulns": 6.0,
    "deals": 48.0,
    "sports": 3.0,
    "tech": 12.0,
}
_DEFAULT_HALF_LIFE_HOURS = 12.0


def half_life_hours(category: str | None) -> float:
    cat = (category or "").lower().strip()
    return _HALF_LIFE_HOURS.get(cat, _DEFAULT_HALF_LIFE_HOURS)


def score(
    published_at: dt.datetime | None,
    category: str | None = None,
    now: dt.datetime | None = None,
) -> float:
    """Exponential decay from 100 toward 0 as the entry ages.

    ``published_at`` is treated as UTC if naive. ``category`` selects
    the half-life; unknown categories use the default.
    """
    if published_at is None:
        return 0.0
    now = now or dt.datetime.now(dt.timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=dt.timezone.utc)
    age_hours = max(0.0, (now - published_at).total_seconds() / 3600.0)
    half_life = half_life_hours(category)
    return 100.0 * math.exp(-age_hours / half_life)

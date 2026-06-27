"""Recency-only scoring for phase 1.

Returns a score in [0, 100]. 100 = just published; 0 = 24h+ old or unknown
date. Used as both raw_score and composite_score in phase 1. Replaced in
phase 2 by a richer scorer that also considers source weight, category
weight, and personal vector similarity.
"""

from __future__ import annotations

import datetime as dt
import math

_HALF_LIFE_HOURS = 6.0


def score(published_at: dt.datetime | None, now: dt.datetime | None = None) -> float:
    if published_at is None:
        return 0.0
    now = now or dt.datetime.now(dt.timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=dt.timezone.utc)
    age_hours = max(0.0, (now - published_at).total_seconds() / 3600.0)
    return 100.0 * math.exp(-age_hours / _HALF_LIFE_HOURS)

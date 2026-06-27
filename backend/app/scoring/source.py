"""Source-weight helpers.

A source's ``source_weight`` is a multiplier on its raw_score when
computing composite_score. Default 1.0 (neutral). Phase 3 will ship
the UI for tuning this per source. Helpers here just centralize the
default lookup so callers don't repeat the SQL fallback.
"""

from __future__ import annotations

from app.models import Source

DEFAULT_WEIGHT = 1.0


def weight(source: Source | None) -> float:
    """Return the source's weight, or the default if source is None."""
    if source is None:
        return DEFAULT_WEIGHT
    w = getattr(source, "source_weight", None)
    return float(w) if w is not None else DEFAULT_WEIGHT

"""Scoring engine package.

Phase 2 exposes three signals — recency, source weight, personal vector +
category preference — blended into a single ``composite_score`` by
``composite.score``. Callers (the scheduler at ingest time, the foryou
endpoint at query time) import the function they need; ``composite`` is
the only one most of them touch.
"""

from app.scoring import personal, recency, source
from app.scoring.composite import convergence_multiplier, score, title_slug
from app.scoring.personal import score as personal_score
from app.scoring.recency import half_life_hours, score as recency_score
from app.scoring.source import weight as source_weight

__all__ = [
    "convergence_multiplier",
    "half_life_hours",
    "personal",
    "personal_score",
    "recency",
    "recency_score",
    "score",
    "source",
    "source_weight",
    "title_slug",
]

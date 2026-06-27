"""Scoring engine package.

Phase 1 ships a deterministic recency-only scorer so cards have a real
composite_score for ordering. Phase 2 adds source weight, recency decay
curves per category, personal vector similarity, and cross-source
convergence boosts.
"""
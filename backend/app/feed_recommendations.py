"""Feed recommendations.

Feeds the user might want to add, served from the
``feed_recommendation_candidates`` table and shown minus anything the
user has already added. Adding fires ``POST /api/sources`` which uses
the dynamic-source path — RSS rows go through
``backend/app/sources/dynamic_rss.py``; ``type="reddit"`` rows go
through ``backend/app/sources/dynamic_reddit.py``.

The pool has two sources (the row's ``source`` column):
    - ``"editorial"`` — the original 28 hand-picked feeds, seeded by
      ``alembic/versions/0017_feed_recommendation_candidates.py``.
      Was a hardcoded ``RECOMMENDATIONS`` list prior to that
      migration; see the migration's docstring for the full seed and
      the conventions each row follows (naming, category, blurb).
    - ``"llm"`` — rows added by ``app.feed_discovery``, either
      triggered automatically when the user adds a custom source
      (nearest-neighbor expansion) or on demand via
      ``POST /api/feed-recommendations/discover``.

Because the pool is DB-backed, updating or adding to it is a row
insert/update, not a code change + restart. ``recommendations_for_user``
re-ranks whatever's currently ``active`` by interaction co-occurrence
and embedding similarity once the user has accumulated engagement
signals — see the "Ranking" section below, unchanged by the storage
migration.

Ranking (``recommendations_for_user``):
    Blends two independent signals, each rescaled to ``[0, 1]``:

    1. Category co-occurrence — aggregate per-source-category scores
       from the last 30 days of ``Interaction`` rows, negate
       ``thumb_down`` / ``never`` events, and squeeze through
       ``tanh`` so a single hot category doesn't dominate.
    2. Vector similarity — cosine similarity between the user's
       ``UserProfile.preference_vector`` (the same one that ranks
       For You) and a one-time sentence embedding of each
       candidate's name + blurb. This is what makes the ranking
       track "your algorithm" rather than just a coarse category
       tally: two feeds in the same curated category can still
       come out in a different order if one's editorial blurb
       reads closer to what you actually engage with.

    ``_CATEGORY_WEIGHT`` / ``_VECTOR_WEIGHT`` control the blend.
    Either signal defaults to neutral (0 co-occurrence, 0.5
    similarity) when it isn't available yet (no interactions, no
    preference vector, or embeddings disabled) — so a fresh install
    with neither serves the curated (editorial) order untouched,
    and a user with only one signal still gets ranked by it alone.
    Ties fall back to ``curated_index asc`` so the list stays
    stable when both signals agree.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings import embedder
from app.models import Entry, FeedRecommendationCandidate, Interaction, Source, UserProfile
from app.scoring.personal import vector_score


async def recommendations_for(
    session: AsyncSession, active_source_names: list[str]
) -> list[dict]:
    """Active candidates, minus anything the user has already added.

    ``active_source_names`` is the list of source ``name`` strings
    the user currently has rows for (built-in + dynamic). Filtering
    on the server side prevents a stale client from showing
    "Add BBC News" duplicates.

    Returns a fresh list of dicts (one per row, shaped for
    ``FeedRecommendation``) ordered by ``id`` ascending — editorial
    rows were seeded in curated order and keep low ids, so this
    preserves the original curated ordering as the tie-break while
    letting newly discovered (``source="llm"``) rows sort after them
    absent a ranking signal.
    """
    active = set(active_source_names or [])
    stmt = (
        select(FeedRecommendationCandidate)
        .where(FeedRecommendationCandidate.active.is_(True))
        .order_by(FeedRecommendationCandidate.id.asc())
    )
    rows = (await session.scalars(stmt)).all()
    return [_candidate_to_dict(row) for row in rows if row.name not in active]


def _candidate_to_dict(row: FeedRecommendationCandidate) -> dict:
    """``FeedRecommendationCandidate`` row -> plain dict shaped for
    ``FeedRecommendation``. ``type`` defaults to "rss" when NULL to
    match the schema's default (the column is nullable so editorial
    RSS rows don't carry a redundant "rss" string)."""
    return {
        "_id": row.id,
        "name": row.name,
        "category": row.category,
        "url": row.url,
        "blurb": row.blurb,
        "type": row.type or "rss",
        "default_headers": row.default_headers,
        "source": row.source,
    }


# Event types that subtract from a category's net score. Listed here
# once so the SQL stays readable; the route layer's InteractionType
# enum shares these names.
_NEGATIVE_TYPES = ("thumb_down", "never")

# Window for the co-occurrence aggregate. Matches the recency decay
# used elsewhere (brief scheduling, brief deduplication); a 30-day
# window means a one-off click from three months ago won't move the
# recommendation needle.
_LOOKBACK = timedelta(days=30)

# Divisor inside tanh: a raw score of 5 in a category maps to
# tanh(5/5) ≈ 0.76; 10 maps to tanh(2) ≈ 0.96. Larger values stay
# close to 1.0 so the ordering stays stable at the top.
_TANH_DIVISOR = 5.0


# Sentinel user_ids soft-auth routes fall back to when a request
# isn't attributable to a stable OIDC identity: "anonymous" (no
# session, bypass off or out of range), "local-bypass" (LAN
# bypass), and "default" (legacy rows predating soft auth). One
# physical user's browser can land under any of the three depending
# on which auth path a given request happened to take (see
# ``app.auth.deps.resolve_user_id``), so scoring has to aggregate
# over all three rather than picking just one — mirrors
# ``app.scheduler._AGGREGATION_USER_IDS_ALL``, which the preference-
# vector recompute already gets right.
_SOFT_AUTH_USER_IDS: tuple[str, ...] = ("anonymous", "local-bypass", "default")


def aggregation_user_ids(user: dict | None) -> tuple[str, ...]:
    """User ids whose interactions should feed this caller's recommendation
    scoring.

    A genuine OIDC identity is scoped to just its own ``sub`` — in a
    multi-user deployment, one user's interaction history shouldn't
    bleed into another's recommendations. Anonymous / local-bypass /
    pre-soft-auth traffic all collapse onto the shared sentinel
    family instead, since in the single-user deployment this app is
    designed for, they're all the same person arriving via different
    auth paths.
    """
    if user is not None and user.get("auth_method") == "oidc":
        return (user["sub"],)
    return _SOFT_AUTH_USER_IDS


async def _category_scores(
    session: AsyncSession, user_ids: tuple[str, ...]
) -> dict[str, float]:
    """Return ``{category: net_score}`` for the user(s), rolled up across
    the last ``_LOOKBACK`` window. ``net_score`` is the sum of
    ``+1`` per positive interaction and ``-1`` per ``thumb_down`` /
    ``never``. Categories with no engagement don't appear.

    ``user_ids`` is plural — see ``aggregation_user_ids`` — so a
    single person's interactions recorded under different sentinel
    ids (or a real OIDC sub) all count toward the same score.

    Negative types live as constants so the SQL reads cleanly. We do
    the negation in SQL rather than fetching rows and folding in
    Python — cheaper at the typical engagement volume (hundreds per
    user, not millions).
    """
    cutoff = datetime.now(timezone.utc) - _LOOKBACK
    # Build the per-row sign once so the aggregate stays readable.
    # ``case`` translates to a portable ``CASE WHEN`` across both
    # SQLite (tests) and Postgres (prod); ``func.IF`` would be MySQL.
    sign = case(
        (Interaction.type.in_(_NEGATIVE_TYPES), -1.0),  # type: ignore[arg-type]
        else_=1.0,
    )
    net_expr = func.sum(sign)
    stmt = (
        select(Source.category.label("category"), net_expr.label("net_score"))
        .join(Entry, Entry.source_id == Source.id)
        .join(Interaction, Interaction.entry_id == Entry.id)
        .where(Interaction.user_id.in_(user_ids))
        .where(Interaction.created_at >= cutoff)
        .group_by(Source.category)
    )
    rows = (await session.execute(stmt)).all()
    return {row.category: float(row.net_score or 0.0) for row in rows}


async def top_category_for_user(session: AsyncSession, user_ids: tuple[str, ...]) -> str | None:
    """The category with the highest net interaction score for
    ``user_ids`` over the last ``_LOOKBACK`` window, or None if there's
    no engagement signal yet. Used by ``POST /api/feed-recommendations/
    discover`` to infer a category when the caller doesn't name one —
    "find more feeds like the ones I already engage with."
    """
    raw_scores = await _category_scores(session, user_ids)
    if not raw_scores:
        return None
    return max(raw_scores.items(), key=lambda kv: kv[1])[0]


def _squeeze(raw: float) -> float:
    """Map raw net-score to ``[0, 1]`` via ``tanh`` so a single hot
    category can't pull every recommendation toward it. Output is
    non-negative because all candidates with a net score below 0 are
    sorted by their (already clamped) value."""
    if raw <= 0:
        return 0.0
    return math.tanh(raw / _TANH_DIVISOR)


# Blend weights for the two ranking signals — see the module
# docstring's "Ranking" section. Slightly favors the vector signal:
# it's continuous and per-candidate (distinguishes two feeds in the
# same curated category), where category co-occurrence is coarser
# and only as fine-grained as the curated list's category labels.
_CATEGORY_WEIGHT = 0.4
_VECTOR_WEIGHT = 0.6


async def _ensure_candidate_embeddings(
    session: AsyncSession, candidates: list[dict]
) -> dict[str, list[float]]:
    """Embed any candidate whose DB row has a NULL ``embedding``,
    persist the result, and return ``{name: embedding}`` for every
    candidate passed in (including ones that already had one).

    Text is ``"<name> <category>: <blurb>"`` — the same shape of
    signal (short description, no boilerplate) the entry embedding
    pipeline feeds the model elsewhere. Persisting to the row (rather
    than the old in-memory ``_CANDIDATE_EMBEDDINGS`` process cache)
    means a freshly restarted backend doesn't re-embed the whole pool
    on its first request, and a newly discovered LLM row only gets
    embedded once, ever. A no-op batch (nothing missing, or
    embeddings disabled — ``embed_many`` then returns all-``None``
    and every candidate is silently skipped) costs nothing.
    """
    by_name = {c["name"]: c for c in candidates}
    existing = (
        await session.execute(
            select(FeedRecommendationCandidate.name, FeedRecommendationCandidate.embedding)
            .where(FeedRecommendationCandidate.name.in_(by_name.keys()))
        )
    ).all()
    result: dict[str, list[float]] = {
        row.name: row.embedding for row in existing if row.embedding is not None
    }
    missing_names = [name for name in by_name if name not in result]
    if not missing_names:
        return result
    texts = [
        f"{by_name[name]['name'].replace('_', ' ')} {by_name[name]['category']}: {by_name[name]['blurb']}"
        for name in missing_names
    ]
    vectors = await embedder().embed_many(texts)
    for name, vec in zip(missing_names, vectors):
        if vec is None:
            continue
        result[name] = vec
        await session.execute(
            FeedRecommendationCandidate.__table__.update()
            .where(FeedRecommendationCandidate.name == name)
            .values(embedding=vec)
        )
    if any(v is not None for v in vectors):
        await session.commit()
    return result


async def recommendations_for_user(
    session: AsyncSession,
    active_source_names: list[str],
    user_ids: tuple[str, ...],
) -> list[dict]:
    """Re-ranked recommendation list for a user.

    Filtering matches ``recommendations_for`` (skip already-added
    sources). Ranking blends the last-30-days interaction
    co-occurrence across ``user_ids`` (see ``aggregation_user_ids``)
    with cosine similarity between each candidate's embedding and the
    user's ``preference_vector`` — see the module docstring's
    "Ranking" section for the full algorithm and why both signals
    matter. Ties fall back to the pool's insertion order (id asc) so
    the list is stable when there's no data.

    Returns a fresh list — the caller may serialize it directly.
    """
    candidates = await recommendations_for(session, active_source_names)
    if not candidates:
        return candidates

    raw_scores = await _category_scores(session, user_ids)
    squeezed = {cat: _squeeze(score) for cat, score in raw_scores.items()}

    profile = await session.scalar(select(UserProfile).where(UserProfile.id == 1))
    pref_vec = profile.preference_vector if profile else None
    vec_scores: dict[str, float] = {}
    if pref_vec is not None:
        embeddings = await _ensure_candidate_embeddings(session, candidates)
        vec_scores = {
            c["name"]: vector_score(embeddings.get(c["name"]), pref_vec)
            for c in candidates
        }

    if squeezed or vec_scores:
        # Pre-compute the pool order so ties are deterministic.
        for idx, item in enumerate(candidates):
            item.setdefault("_pool_index", idx)

        def _combined_score(item: dict) -> float:
            cat_component = squeezed.get(item["category"], 0.0)  # 0..1, 0 = no signal
            # vector_score is 0..100 with 50 as its own "no signal"
            # neutral; rescale to 0..1 so the two components are
            # comparable and neither dominates just from its native range.
            vec_component = vec_scores.get(item["name"], 50.0) / 100.0
            return _CATEGORY_WEIGHT * cat_component + _VECTOR_WEIGHT * vec_component

        # Sort by descending combined score, then by pool index ascending.
        candidates.sort(key=lambda item: (-_combined_score(item), item["_pool_index"]))
        for item in candidates:
            item.pop("_pool_index", None)
    # else: neither signal available yet (no interactions, no
    # preference vector) — fall through to pool order, which is
    # already what ``recommendations_for`` returned.

    # Strip the internal row-id key before returning — the API
    # serializer (``FeedRecommendation``) doesn't know about it.
    for item in candidates:
        item.pop("_id", None)
    return candidates
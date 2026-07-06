"""Regression test for the "scores freeze at ingest time and never
decay" bug fixed in PR #12 (``scheduler._rescore_recent_entries``).

Recreates the exact scenario found via manual verification during
development: a stale entry whose embedding doesn't match the user's
taste but was scored high at ingest (frozen forever without this job),
versus a fresh entry whose embedding matches the user's taste. Also
exercises the actual SQLAlchemy bulk-update statement — this is where
the original implementation hit "ORM Bulk UPDATE by Primary Key"
errors (see ``_rescore_recent_entries``'s docstring), so a test that
only checked the returned *scores* without going through the real
``Entry.__table__.update()`` bulk-update path would have missed that
class of bug entirely.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import scheduler
from app.models import UserProfile
from factories import make_entry, make_source


@pytest.mark.asyncio
async def test_rescore_demotes_stale_mismatched_entry_and_promotes_fresh_matching_one(
    db_session, make_vector
):
    source = await make_source(db_session, "wikipedia_on_this_day", category="misc")

    liked_vec = make_vector(1.0)
    unrelated_vec = make_vector(-1.0)

    db_session.add(UserProfile(id=1, preference_vector=liked_vec))
    await db_session.commit()

    now = dt.datetime.now(dt.timezone.utc)
    stale_mismatched = await make_entry(
        db_session,
        source,
        "On This Day: something from 1850",
        # published_at looks fresh (Wikipedia On This Day sets it to
        # fetch time, not the historical event's date) but fetched_at
        # is genuinely old.
        published_at=now,
        fetched_at=now - dt.timedelta(days=3),
        composite_score=85.0,
        personal_score=90.0,
        raw_score=85.0,
        embedding=unrelated_vec,
    )
    fresh_matching = await make_entry(
        db_session,
        source,
        "A story matching the user's taste",
        published_at=now - dt.timedelta(hours=2),
        fetched_at=now - dt.timedelta(hours=2),
        composite_score=70.0,
        personal_score=50.0,
        raw_score=70.0,
        embedding=liked_vec,
    )

    # Sanity check on the *pre*-rescore state: the stale mismatched
    # entry currently outranks the fresh matching one, which is
    # exactly the bug this job corrects.
    assert stale_mismatched.composite_score > fresh_matching.composite_score

    await scheduler._rescore_recent_entries()

    await db_session.refresh(stale_mismatched)
    await db_session.refresh(fresh_matching)

    assert fresh_matching.composite_score > stale_mismatched.composite_score
    assert fresh_matching.personal_score > stale_mismatched.personal_score


@pytest.mark.asyncio
async def test_rescore_only_touches_entries_within_the_window(db_session, make_vector):
    source = await make_source(db_session, "old_source", category="misc")
    now = dt.datetime.now(dt.timezone.utc)

    too_old = await make_entry(
        db_session,
        source,
        "Ancient entry outside the rescore window",
        fetched_at=now - dt.timedelta(days=scheduler._RESCORE_WINDOW_DAYS + 1),
        composite_score=12.34,
        personal_score=56.78,
        embedding=make_vector(0.5),
    )

    await scheduler._rescore_recent_entries()

    await db_session.refresh(too_old)
    assert too_old.composite_score == 12.34
    assert too_old.personal_score == 56.78


@pytest.mark.asyncio
async def test_rescore_uses_core_table_update_not_orm_bulk_by_pk(db_session, make_vector):
    """Guards the exact bug class the docstring calls out: using
    ``update(Entry)`` (the ORM-mapped class) instead of
    ``Entry.__table__.update()`` (Core) makes SQLAlchemy try to
    auto-detect "ORM Bulk UPDATE by Primary Key" mode, which requires
    the update dict's key to be named exactly ``id`` — this job's
    bindparam is named ``_id`` on purpose to avoid that. A regression
    here raises ``InvalidRequestError`` instead of silently mis-scoring."""
    source = await make_source(db_session, "some_source", category="misc")
    entry = await make_entry(
        db_session,
        source,
        "Needs a rescore",
        composite_score=0.0,
        personal_score=0.0,
        embedding=make_vector(0.1),
    )

    await scheduler._rescore_recent_entries()  # must not raise

    # ``db_session`` still has ``entry`` in its identity map from the
    # insert above (``expire_on_commit=False``, matching production's
    # ``SessionLocal``) — re-querying by id would just hand back the
    # same cached Python object with its original attribute values.
    # ``refresh`` forces a real re-read of the row the rescore job
    # (a separate session) wrote to.
    await db_session.refresh(entry)
    assert entry.composite_score != 0.0 or entry.personal_score != 0.0

"""``app.ingest_rules.select_items_for_ingest``: which fetched items an
ingest keeps, as a pure function. The same rules run end to end
through ``scheduler._ingest`` in ``test_ingest_backfill_cap.py``."""

from __future__ import annotations

import datetime as dt

import pytest

from app import ingest_rules
from app.ingest_rules import select_items_for_ingest

pytestmark = pytest.mark.no_db

NOW = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.timezone.utc)


def _item(n: int, *, hours_ago: float | None) -> dict:
    when = None if hours_ago is None else NOW - dt.timedelta(hours=hours_ago)
    return {"title": f"Item {n}", "url": f"https://feed.test/{n}", "published_at": when}


def _urls(items: list[dict]) -> list[str]:
    return [i["url"] for i in items]


def _select(items, **kw):
    kw.setdefault("known", set())
    kw.setdefault("baseline_at", NOW - dt.timedelta(days=30))
    kw.setdefault("now", NOW)
    return select_items_for_ingest(items, **kw)


# --- first fetch -------------------------------------------------------


def test_first_fetch_keeps_the_newest_10_plus_the_last_2_days():
    # 30 items, one every 12 h: 4 inside the last 48 h, 26 older.
    items = [_item(n, hours_ago=n * 12 + 1) for n in range(30)]
    kept, counts = _select(items, first_fetch=True)
    assert _urls(kept) == _urls(items[:10])
    assert counts == {ingest_rules.FIRST_FETCH: 20}


def test_first_fetch_keeps_everything_from_the_last_2_days_beyond_10():
    # 30 items inside the last 2 days, 10 older ones.
    recent = [_item(n, hours_ago=1 + n) for n in range(30)]
    old = [_item(100 + n, hours_ago=24 * 10 + n) for n in range(10)]
    kept, counts = _select(recent + old, first_fetch=True)
    assert _urls(kept) == _urls(recent)
    assert counts == {ingest_rules.FIRST_FETCH: 10}


def test_first_fetch_ranks_by_date_not_feed_order_and_undated_last():
    # Feed order is scrambled; the newest 3 by date win, undated last.
    items = [
        _item(1, hours_ago=None),
        _item(2, hours_ago=24 * 9),
        _item(3, hours_ago=24 * 5),
        _item(4, hours_ago=24 * 7),
        _item(5, hours_ago=24 * 6),
    ]
    kept, counts = _select(items, first_fetch=True, keep_first=3)
    # Kept in feed order.
    assert _urls(kept) == ["https://feed.test/3", "https://feed.test/4", "https://feed.test/5"]
    assert counts == {ingest_rules.FIRST_FETCH: 2}


def test_first_fetch_of_an_undated_feed_keeps_the_top_10():
    items = [_item(n, hours_ago=None) for n in range(60)]
    kept, counts = _select(items, first_fetch=True)
    assert _urls(kept) == _urls(items[:10])
    assert counts == {ingest_rules.FIRST_FETCH: 50}


def test_first_fetch_ignores_baseline_and_max_age():
    # A dead blog: the newest stories are a year old. A first fetch
    # still shows them (newest 10).
    items = [_item(n, hours_ago=24 * 365 + n) for n in range(12)]
    kept, counts = _select(items, first_fetch=True, baseline_at=NOW)
    assert len(kept) == 10
    assert counts == {ingest_rules.FIRST_FETCH: 2}


def _weekly(n: int, *, weeks_ago: float, extra_h: float = 0) -> dict:
    return _item(n, hours_ago=1 + extra_h + weeks_ago * 7 * 24)


@pytest.mark.parametrize("new_posts", [0, 4])
def test_resumed_first_fetch_does_not_pull_back_the_skipped_back_catalogue(new_posts):
    """A weekly blog lists 20 posts; its first fetch stored the newest
    10. Four weeks later (a resume) it lists ``new_posts`` new posts
    plus its 16 newest old ones. The newest 10 are ranked over the
    whole listing, so the stored posts take their slots and the old
    posts below them stay out."""
    gap_h = 28 * 24
    old = [_weekly(n, weeks_ago=n, extra_h=gap_h) for n in range(16)]
    new = [_weekly(100 + n, weeks_ago=n) for n in range(new_posts)]
    stored = set(_urls(old[:10]))
    kept, counts = _select(new + old, known=stored, first_fetch=True)
    assert _urls(kept) == _urls(new)
    assert counts == {ingest_rules.FIRST_FETCH: 6}


@pytest.mark.parametrize("new_posts", [0, 4])
def test_resumed_first_fetch_of_an_undated_feed_stops_at_the_stored_ones(new_posts):
    old = [_item(n, hours_ago=None) for n in range(20)]
    new = [_item(100 + n, hours_ago=None) for n in range(new_posts)]
    stored = set(_urls(old[:10]))
    kept, counts = _select(new + old, known=stored, first_fetch=True)
    assert _urls(kept) == _urls(new)
    assert counts == {ingest_rules.FIRST_FETCH: 10}


def test_new_feed_overlapping_stored_stories_keeps_only_its_newest_10():
    # Another feed already stored this feed's 6 newest stories. The new
    # feed's first fetch fills the 4 slots left, not 10 older ones.
    items = [_item(n, hours_ago=1 + n * 24 * 3) for n in range(30)]
    kept, counts = _select(items, known=set(_urls(items[:6])), first_fetch=True)
    assert _urls(kept) == _urls(items[6:10])
    assert counts == {ingest_rules.FIRST_FETCH: 20}


# --- later polls -------------------------------------------------------


def test_later_poll_keeps_a_new_item_dated_5_days_back():
    known_item = _item(0, hours_ago=1)
    late = _item(1, hours_ago=24 * 5)
    kept, counts = _select(
        [known_item, late],
        known={known_item["url"]},
        first_fetch=False,
        baseline_at=NOW - dt.timedelta(days=20),
    )
    assert _urls(kept) == [late["url"]]
    assert counts == {}


def test_later_poll_drops_items_dated_before_the_baseline_window():
    # Baseline 1 day ago: anything before 3 days ago is the first
    # fetch's backfill and stays out.
    items = [_item(1, hours_ago=24 * 2.5), _item(2, hours_ago=24 * 3.5)]
    kept, counts = _select(items, first_fetch=False, baseline_at=NOW - dt.timedelta(days=1))
    assert _urls(kept) == ["https://feed.test/1"]
    assert counts == {ingest_rules.BEFORE_BASELINE: 1}


def test_later_poll_drops_items_older_than_max_age():
    items = [_item(1, hours_ago=24 * 29), _item(2, hours_ago=24 * 31)]
    kept, counts = _select(items, first_fetch=False, baseline_at=NOW - dt.timedelta(days=365))
    assert _urls(kept) == ["https://feed.test/1"]
    assert counts == {ingest_rules.TOO_OLD: 1}


def test_later_poll_keeps_at_most_2_undated_above_the_first_known_url():
    new_top = [_item(n, hours_ago=None) for n in range(5)]
    known_items = [_item(10 + n, hours_ago=None) for n in range(10)]
    backlog = [_item(30 + n, hours_ago=None) for n in range(20)]
    items = new_top + known_items + backlog
    kept, counts = _select(items, known=set(_urls(known_items)), first_fetch=False)
    # The topmost (newest) two of the five new ones.
    assert _urls(kept) == _urls(new_top[:2])
    assert counts == {ingest_rules.UNDATED: 3 + 20}


def test_later_poll_drops_undated_backlog_below_the_first_known_url():
    known_items = [_item(n, hours_ago=None) for n in range(10)]
    backlog = [_item(10 + n, hours_ago=None) for n in range(50)]
    kept, counts = _select(
        known_items + backlog, known=set(_urls(known_items)), first_fetch=False
    )
    assert kept == []
    assert counts == {ingest_rules.UNDATED: 50}


def test_undated_cap_follows_the_knob():
    items = [_item(n, hours_ago=None) for n in range(4)]
    kept, counts = _select(items, first_fetch=False, max_undated=0)
    assert kept == [] and counts == {ingest_rules.UNDATED: 4}
    kept, counts = _select(items, first_fetch=False, max_undated=3)
    assert _urls(kept) == _urls(items[:3])


def test_dated_items_are_not_limited_by_the_known_position():
    # A dated story below the first known URL (a feed that re-sorts,
    # or an edit that bumped an old story) is judged by its date only.
    known_item = _item(0, hours_ago=1)
    below = _item(1, hours_ago=3)
    kept, counts = _select([known_item, below], known={known_item["url"]}, first_fetch=False)
    assert _urls(kept) == [below["url"]]
    assert counts == {}


# --- per-poll cap ------------------------------------------------------


def test_per_poll_cap_keeps_the_newest_and_leaves_the_rest_for_later():
    items = [_item(n, hours_ago=1 + n * 0.1) for n in range(80)]
    kept, counts = _select(items, first_fetch=False)
    assert _urls(kept) == _urls(items[:50])
    assert counts == {ingest_rules.PER_POLL: 30}
    # Next poll: the 50 are stored now, the other 30 land.
    kept2, counts2 = _select(items, known=set(_urls(kept)), first_fetch=False)
    assert _urls(kept2) == _urls(items[50:])
    assert counts2 == {}


def test_per_poll_cap_applies_after_the_date_rules():
    # 60 too-old items don't use up the cap.
    old = [_item(n, hours_ago=24 * 40 + n) for n in range(60)]
    new = [_item(100 + n, hours_ago=1) for n in range(5)]
    kept, counts = _select(old + new, first_fetch=False, baseline_at=NOW - dt.timedelta(days=365))
    assert _urls(kept) == _urls(new)
    assert counts == {ingest_rules.TOO_OLD: 60}


def test_per_poll_cap_also_bounds_a_first_fetch():
    items = [_item(n, hours_ago=0.5 + n * 0.1) for n in range(70)]
    kept, counts = _select(items, first_fetch=True)
    assert _urls(kept) == _urls(items[:50])
    assert counts == {ingest_rules.PER_POLL: 20}


def test_per_poll_cap_ranks_by_date_not_feed_order():
    # Listed oldest first (a sitemap, a re-sorted feed): the cap still
    # keeps the 50 newest by date, returned in feed order.
    items = [_item(n, hours_ago=1 + (70 - n) * 0.5) for n in range(70)]
    kept, counts = _select(items, first_fetch=False)
    assert _urls(kept) == _urls(items[20:])
    assert counts == {ingest_rules.PER_POLL: 20}


def test_per_poll_cap_ranks_by_date_on_a_first_fetch_too():
    items = [_item(n, hours_ago=0.5 + (70 - n) * 0.1) for n in range(70)]
    kept, counts = _select(items, first_fetch=True)
    assert _urls(kept) == _urls(items[20:])
    assert counts == {ingest_rules.PER_POLL: 20}


def test_undated_items_get_per_poll_slots_before_dated_ones():
    """60 new dated stories, then 2 new undated ones, above the stored
    part of the listing. Over two polls all 62 land: the undated ones
    can't wait, because once the dated ones above them are stored they
    sit below a known URL."""
    dated = [_item(n, hours_ago=1 + n * 0.25) for n in range(60)]
    undated = [_item(100 + n, hours_ago=None) for n in range(2)]
    stored = [_item(200 + n, hours_ago=30 + n) for n in range(5)]
    items = dated + undated + stored
    known = set(_urls(stored))

    kept, counts = _select(items, known=known, first_fetch=False)
    assert set(_urls(undated)) <= set(_urls(kept))
    assert _urls(kept) == _urls(dated[:48] + undated)
    assert counts == {ingest_rules.PER_POLL: 12}

    known |= set(_urls(kept))
    kept2, counts2 = _select(items, known=known, first_fetch=False)
    assert _urls(kept2) == _urls(dated[48:])
    assert counts2 == {}
    assert known | set(_urls(kept2)) == set(_urls(items))


# --- duplicates and counting --------------------------------------------


def test_known_and_repeated_urls_are_duplicates_not_drops():
    a = _item(1, hours_ago=1)
    b = _item(2, hours_ago=2)
    items = [a, b, dict(b), _item(3, hours_ago=3)]
    kept, counts = _select(items, known={a["url"]}, first_fetch=False)
    assert _urls(kept) == [b["url"], "https://feed.test/3"]
    assert counts == {}


@pytest.mark.parametrize("first_fetch", [True, False])
def test_every_item_is_kept_duplicate_or_counted(first_fetch):
    items = (
        [_item(n, hours_ago=None) for n in range(7)]
        + [_item(10 + n, hours_ago=1 + n) for n in range(60)]
        + [_item(100 + n, hours_ago=24 * 3 + n) for n in range(20)]
        + [_item(200 + n, hours_ago=24 * 45 + n) for n in range(5)]
    )
    items.insert(30, dict(items[12]))  # a repeat within the listing
    known = {items[5]["url"], items[40]["url"]}
    kept, counts = _select(
        items, known=known, first_fetch=first_fetch, baseline_at=NOW - dt.timedelta(days=2)
    )
    unique_new = {i["url"] for i in items} - known
    assert len(kept) + sum(counts.values()) == len(unique_new)
    assert len({i["url"] for i in kept}) == len(kept)
    assert not ({i["url"] for i in kept} & known)


# --- published_at shapes ------------------------------------------------


def test_naive_and_string_dates_are_read_as_utc():
    naive = {"url": "https://feed.test/a", "published_at": dt.datetime(2026, 10, 19, 11, 0)}
    iso = {"url": "https://feed.test/b", "published_at": "2026-10-19T10:00:00Z"}
    junk = {"url": "https://feed.test/c", "published_at": "yesterday-ish"}
    assert ingest_rules.published_utc(naive["published_at"]) == NOW - dt.timedelta(hours=1)
    assert ingest_rules.published_utc(iso["published_at"]) == NOW - dt.timedelta(hours=2)
    assert ingest_rules.published_utc(junk["published_at"]) is None
    # An unreadable date counts as undated.
    kept, counts = _select([naive, iso, junk], known=set(), first_fetch=False, max_undated=0)
    assert _urls(kept) == ["https://feed.test/a", "https://feed.test/b"]
    assert counts == {ingest_rules.UNDATED: 1}


# --- tunables -----------------------------------------------------------


def test_tunables_default_when_unset():
    assert ingest_rules.parse_tunables({}) == ingest_rules.IngestTunables()
    assert ingest_rules.parse_tunables({k: None for k in ingest_rules.TUNABLE_KEYS}) == (
        ingest_rules.IngestTunables()
    )
    defaults = ingest_rules.IngestTunables()
    assert (
        defaults.first_fetch_keep,
        defaults.backfill_hours,
        defaults.max_age_days,
        defaults.max_per_poll,
        defaults.resume_gap_hours,
        defaults.max_undated_per_poll,
    ) == (10, 48, 30, 50, 72, 2)


def test_tunables_parse_and_fall_back_when_out_of_range():
    t = ingest_rules.parse_tunables(
        {
            "ingest.first_fetch_keep": "20",
            "ingest.backfill_hours": " 24 ",
            "ingest.max_age_days": "0",          # below range → default
            "ingest.max_per_poll": "lots",       # not a number → default
            "ingest.resume_gap_hours": "24",     # under the 48 h floor → default
            "ingest.max_undated_per_poll": "0",  # 0 is allowed
        }
    )
    assert t == ingest_rules.IngestTunables(
        first_fetch_keep=20,
        backfill_hours=24,
        max_age_days=30,
        max_per_poll=50,
        resume_gap_hours=72,
        max_undated_per_poll=0,
    )

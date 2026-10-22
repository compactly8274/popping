"""The ``preview`` field of the card projection (PR5a, D34).

Every list endpoint (``/api/entries``, its search, ``/api/foryou`` and
``/api/entries/by-ids``) sends a short blurb for the card to show
under the headline: the cached AI summary when there is one, else the
feed's own description cleaned and cut to about 300 characters, or
null when there is nothing worth showing. It is computed in the
projection (``routes.entries.card_preview``): no column, no
migration.
"""

from __future__ import annotations

import pytest

from app.routes.entries import _PREVIEW_MAX_CHARS, card_preview
from factories import make_entry, make_source

# ---------------------------------------------------------------------------
# The pure rules
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_cached_summary_wins_over_the_feed_blurb():
    assert card_preview("Title", "The AI summary.", "<p>The feed blurb.</p>") == "The AI summary."
    # An empty cached summary ("asked, nothing") falls through.
    assert card_preview("Title", "", "<p>The feed blurb.</p>") == "The feed blurb."
    assert card_preview("Title", "   ", "<p>The feed blurb.</p>") == "The feed blurb."


@pytest.mark.no_db
def test_feed_blurb_is_cleaned():
    raw = "<p>Tags &amp; entities,<br/>  and\n\n whitespace&#8217;s   collapsed.</p>"
    assert card_preview("Title", None, raw) == "Tags & entities, and whitespace’s collapsed."


@pytest.mark.no_db
def test_cut_on_a_word_boundary_at_300():
    words = " ".join(f"word{i}" for i in range(200))
    out = card_preview("Title", None, words)
    assert out is not None
    assert out.endswith("…")
    assert len(out) <= _PREVIEW_MAX_CHARS + 1
    # Whole words only: the text before the ellipsis is a prefix of
    # the input that ends at a space.
    body = out[:-1]
    assert words.startswith(body)
    assert words[len(body)] == " "
    # Short text is left alone.
    assert card_preview("Title", None, "short") == "short"
    # A long cached summary (the endpoint caps it at 800) is cut the
    # same way: the preview is a blurb, the Summary arrow has the rest.
    long_cached = " ".join(f"ai{i}" for i in range(200))
    cut = card_preview("Title", long_cached, None)
    assert cut is not None and cut.endswith("…") and len(cut) <= _PREVIEW_MAX_CHARS + 1
    assert long_cached.startswith(cut[:-1])


@pytest.mark.no_db
def test_a_blurb_that_repeats_the_title_is_dropped():
    assert card_preview("Council approves 2027 capital plan", None, "Council approves 2027 capital plan") is None
    # Case, punctuation and spacing don't make it a different text.
    assert card_preview("Council approves 2027 capital plan", None, "<b>COUNCIL</b> approves 2027 capital-plan!") is None
    assert card_preview("Council approves 2027 capital plan", "council approves 2027 capital plan.", None) is None
    # More than the title is a preview.
    assert card_preview("Council approves 2027 capital plan", None, "Council approves 2027 capital plan after a long night.") == (
        "Council approves 2027 capital plan after a long night."
    )


@pytest.mark.no_db
def test_reddit_link_post_trailer_is_dropped():
    link_post = (
        '&#32; submitted by &#32; <a href="https://www.reddit.com/user/someone"> /u/someone </a> <br/> '
        '<span><a href="https://example.com/article">[link]</a></span> &#32; '
        '<span><a href="https://www.reddit.com/r/x/comments/abc/">[comments]</a></span>'
    )
    assert card_preview("A link post", None, link_post) is None
    # A self post keeps the OP's text and loses the trailer.
    self_post = '<!-- SC_OFF --><div class="md"><p>What the OP wrote.</p></div><!-- SC_ON -->' + link_post
    assert card_preview("A self post", None, self_post) == "What the OP wrote."


@pytest.mark.no_db
def test_nothing_to_show_is_none():
    assert card_preview("Title", None, None) is None
    assert card_preview("Title", None, "") is None
    assert card_preview("Title", "", "<p> </p>") is None
    assert card_preview(None, None, None) is None


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------


async def _seed(db_session, category: str = "news"):
    feed = await make_source(db_session, f"preview_feed_{category}", category=category)
    blurb = await make_entry(db_session, feed, "Blurb story", composite_score=50.0)
    blurb.meta = {"summary": "<p>The feed&#8217;s own <b>blurb</b> text.</p>"}
    cached = await make_entry(db_session, feed, "Cached story", composite_score=40.0)
    cached.cached_summary = "The AI summary."
    cached.meta = {"summary": "<p>Not this one.</p>"}
    bare = await make_entry(db_session, feed, "Bare story", composite_score=30.0)
    bare.meta = {"summary": "Bare story"}
    db_session.add_all([blurb, cached, bare])
    await db_session.commit()
    return blurb, cached, bare


def _previews(rows) -> dict[int, str | None]:
    return {r["id"]: r["preview"] for r in rows}


async def test_list_entries_carries_preview(app_client, db_session):
    blurb, cached, bare = await _seed(db_session)
    resp = await app_client.get("/api/entries")
    assert resp.status_code == 200, resp.text
    got = _previews(resp.json())
    assert got[blurb.id] == "The feed’s own blurb text."
    assert got[cached.id] == "The AI summary."
    assert got[bare.id] is None
    # The blurb itself never reaches the response.
    assert all("feed_summary_text" not in r and "meta" not in r for r in resp.json())


async def test_search_and_by_ids_carry_preview(app_client, db_session):
    blurb, cached, bare = await _seed(db_session)
    resp = await app_client.get("/api/entries", params={"q": "story"})
    assert resp.status_code == 200, resp.text
    assert _previews(resp.json())[blurb.id] == "The feed’s own blurb text."

    resp = await app_client.get("/api/entries/by-ids", params={"ids": f"{cached.id},{bare.id},{blurb.id}"})
    assert resp.status_code == 200, resp.text
    assert [r["preview"] for r in resp.json()] == ["The AI summary.", None, "The feed’s own blurb text."]


async def test_for_you_carries_preview(app_client, db_session):
    blurb, cached, bare = await _seed(db_session, category="tech")
    resp = await app_client.get("/api/foryou")
    assert resp.status_code == 200, resp.text
    got = _previews(resp.json())
    assert got[blurb.id] == "The feed’s own blurb text."
    assert got[cached.id] == "The AI summary."
    assert got[bare.id] is None

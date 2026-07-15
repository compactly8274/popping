"""Tests for the HTML-entity double-encoding fix: some feeds (WordPress
ones in particular — The Verge is one) double-encode titles/summaries,
so the XML parse only resolves the outer layer and a literal
``&#8217;`` etc. survives into the text. Covers both call sites that
got the fix: ``app.sources.base.validate_required`` (titles, shared by
every source plugin) and ``app.routes.entries._clean_summary``
(the per-entry summary deck).
"""

from __future__ import annotations

from app.routes.entries import _clean_summary
from app.sources.base import validate_required


def test_validate_required_unescapes_double_encoded_title():
    raw = {"title": "Nomad&#8217;s high-end phone accessories are up to 30 percent off", "url": "https://example.com/a"}
    result = validate_required("the_verge", raw)
    # &#8217; is the curly right-single-quote (U+2019), not a straight
    # apostrophe — unescaping should produce the real typographic
    # character the source intended, not literal entity text.
    assert result["title"] == "Nomad’s high-end phone accessories are up to 30 percent off"


def test_validate_required_leaves_clean_title_unchanged():
    raw = {"title": "Nomad's high-end phone accessories", "url": "https://example.com/a"}
    result = validate_required("the_verge", raw)
    assert result["title"] == "Nomad's high-end phone accessories"


def test_validate_required_unescapes_named_and_numeric_entities():
    raw = {"title": "Q&amp;A: R&amp;D at Acme &mdash; a &quot;deep dive&quot;", "url": "https://example.com/b"}
    result = validate_required("some_source", raw)
    assert result["title"] == 'Q&A: R&D at Acme — a "deep dive"'


def test_clean_summary_unescapes_double_encoded_entities():
    assert _clean_summary("Nomad&#8217;s accessories are on sale") == "Nomad’s accessories are on sale"


def test_clean_summary_strips_tags_and_unescapes():
    assert _clean_summary("<p>Save up to 30% &amp; more</p>") == "Save up to 30% & more"


def test_clean_summary_empty_input_returns_empty_string():
    assert _clean_summary(None) == ""
    assert _clean_summary("") == ""

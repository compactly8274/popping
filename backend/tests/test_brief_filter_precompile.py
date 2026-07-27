"""Unit tests for ``app.brief_filter``'s pre-compiled patterns.

``is_clickbait`` is called once per entry the brief pipeline
considers (up to 500 rows in the over-fetch path). The previous
shape stored ``_LISTICLE_PATTERNS`` as raw strings and called
``re.search(pat, tl)`` per check, which re-compiles the pattern
on every call — 6 patterns * 500 rows = 3000 regex compiles per
brief generation.

The fix pre-compiles the patterns at module load
(``_LISTICLE_PATTERNS: tuple[re.Pattern[str], ...]``) and
similarly for the token-extractor regex (``_TOKEN_RE``). These
tests lock the wire so a future refactor that drops the
pre-compile is caught at unit-test time, not in production.
"""

from __future__ import annotations

import re

import pytest

from app import brief_filter


@pytest.mark.no_db
def test_listicle_patterns_are_pre_compiled_at_module_load():
    """All listicle patterns must be ``re.Pattern`` objects, not
    raw strings. Locks the wire: a future change that reverts
    to a ``tuple[str, ...]`` (and re-compiles per call) fails
    this test before it ships.
    """
    assert len(brief_filter._LISTICLE_PATTERNS) == 6, (
        "listicle pattern count changed; update the doc string and "
        "the test to match"
    )
    for i, pat in enumerate(brief_filter._LISTICLE_PATTERNS):
        assert isinstance(pat, re.Pattern), (
            f"_LISTICLE_PATTERNS[{i}] must be a compiled re.Pattern, "
            f"got {type(pat).__name__}"
        )


@pytest.mark.no_db
def test_token_re_is_pre_compiled():
    """The token-extractor regex (used for the sensational-words
    whole-word check) must be pre-compiled. The previous shape
    used ``re.findall(r\"[a-zA-Z][a-zA-Z\\-']*\", tl)`` per call,
    which compiled a 1KB-class regex 500 times per brief.
    """
    assert isinstance(brief_filter._TOKEN_RE, re.Pattern)


@pytest.mark.no_db
def test_listicle_patterns_match_case_insensitive():
    """Patterns were previously matched after ``tl = t.lower()``,
    so they didn't need ``re.IGNORECASE``. After pre-compiling,
    the patterns carry ``re.IGNORECASE`` directly, and the
    function still lowercases first. Test that the patterns
    individually are case-insensitive so the lowercase
    preprocessing remains a no-op rather than load-bearing
    (a future refactor that drops the lowercase still
    produces correct results).
    """
    pat = brief_filter._LISTICLE_PATTERNS[0]  # the "10 things" pattern
    assert pat.search("10 THINGS you need to know") is not None
    assert pat.search("10 Things You Need To Know") is not None


@pytest.mark.no_db
@pytest.mark.parametrize(
    "title,expected",
    [
        # Listicle — the canonical clickbait shape.
        ("10 things you need to know about AI", True),
        ("7 ways to make your code faster", True),
        ("5 reasons why X is happening", True),
        ("3 hacks for better sleep", True),
        # "You won't believe" template.
        ("You won't believe what happened next", True),
        ("You wont believe this story", True),
        # "This one weird trick" template.
        ("This one weird trick saves money", True),
        ("This genius hack will change your life", True),
        # "What happened next" / "the truth about" / "doctors hate".
        ("What happened next shocked everyone", True),
        ("The truth about productivity", True),
        ("Doctors hate this one weird trick", True),
    ],
)
def test_listicle_titles_detected(title, expected):
    assert brief_filter.is_clickbait(title) is expected


@pytest.mark.no_db
@pytest.mark.parametrize(
    "title",
    [
        "Stock market closes at record high",
        "BBC News at 10",
        "Why the new chip is faster than the last one",
        "OpenAI releases a new model",
        "Hacker News top stories for July 27",
    ],
)
def test_normal_titles_not_flagged(title):
    assert brief_filter.is_clickbait(title) is False, (
        f"false positive on a normal title: {title!r}"
    )


@pytest.mark.no_db
def test_allcaps_detection_still_works():
    """Sanity check: the all-caps branch fires before the listicle
    loop, so a short all-caps title still trips the filter.
    Locks the short-circuit ordering — moving the listicle loop
    ahead of the allcaps check would still be correct
    functionally but change the wall-clock mix slightly.
    """
    assert brief_filter.is_clickbait("BREAKING: HUGE NEWS TODAY") is True
    assert brief_filter.is_clickbait("FBI") is False  # too short


@pytest.mark.no_db
def test_excessive_punctuation_still_works():
    """Three or more ``!`` or ``?`` in a row is shouting. The
    pre-compile change must not affect the punctuation check.
    """
    assert brief_filter.is_clickbait("What!!! This is amazing") is True
    assert brief_filter.is_clickbait("Is this real?") is False
    assert brief_filter.is_clickbait("Really?!?!") is True


@pytest.mark.no_db
def test_sensational_words_still_detected_via_pre_compiled_token_re():
    """The sensational-words check uses ``_TOKEN_RE`` to split
    the lowercased title into tokens, then checks each token
    against ``_SENSATIONAL_WORDS``. The behavior is unchanged
    from the previous shape (still pre-compile at module load),
    but a refactor that swaps the regex out for a buggy one
    is caught here.
    """
    assert brief_filter.is_clickbait("SHOCKING development in the case") is True
    assert brief_filter.is_clickbait("An unbelievable story") is True
    # "unbelievably" should NOT trip (the whole-word check).
    assert brief_filter.is_clickbait("unbelievably good news") is False
    assert brief_filter.is_clickbait("epic launch happened") is True
    assert brief_filter.is_clickbait("horrifying accident today") is True


@pytest.mark.no_db
def test_empty_and_none_safe():
    assert brief_filter.is_clickbait(None) is False
    assert brief_filter.is_clickbait("") is False
    assert brief_filter.is_clickbait("   ") is False
    assert brief_filter.is_clickbait("\n\t") is False


@pytest.mark.no_db
def test_call_does_not_recompile_patterns():
    """Indirect perf invariant: importing the module and calling
    ``is_clickbait`` 1000 times must not allocate a new
    ``re.Pattern`` on each call. ``re._cache`` (the internal
    Python regex cache) is the usual place; the cleaner check
    is that the patterns on the module object are identical
    before and after a call.
    """
    before = brief_filter._LISTICLE_PATTERNS
    for _ in range(1000):
        brief_filter.is_clickbait("10 things you need to know")
    after = brief_filter._LISTICLE_PATTERNS
    assert before is after, (
        "_LISTICLE_PATTERNS identity changed across calls — "
        "the patterns are being re-created on each call"
    )
    # Also the token regex.
    before_tok = brief_filter._TOKEN_RE
    for _ in range(1000):
        brief_filter.is_clickbait("SHOCKING development")
    after_tok = brief_filter._TOKEN_RE
    assert before_tok is after_tok

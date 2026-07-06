"""Unit tests for the source retry/backoff logic added in ``app.scheduler``.

Pure functions only — no DB, no scheduler instance. ``_classify_error``
and the backoff math are the two pieces most likely to silently regress
(e.g. someone reordering the isinstance checks, or the multiplier/cap
constants drifting) since nothing else in the codebase exercises them.
"""

from __future__ import annotations

import httpx
import pytest

from app import scheduler


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com/feed")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"{code} error", request=request, response=response)


@pytest.mark.parametrize(
    "code,expected",
    [
        (401, "blocked"),
        (402, "blocked"),  # RFD's Tollbit paywall gate
        (403, "blocked"),
        (404, "not found"),
        (500, "upstream error"),
        (503, "upstream error"),
        (418, "http error"),  # some other 4xx we don't special-case
    ],
)
def test_classify_error_http_status(code, expected):
    assert scheduler._classify_error(_http_status_error(code)) == expected


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("boom"),
        httpx.ConnectTimeout("boom"),
        httpx.ReadTimeout("boom"),
        httpx.TooManyRedirects("boom"),
    ],
)
def test_classify_error_network(exc):
    assert scheduler._classify_error(exc) == "network"


def test_classify_error_generic_exception_falls_back():
    assert scheduler._classify_error(ValueError("weird")) == "error"


def test_backoff_doubles_and_caps():
    base = 900  # RFD's refresh_interval_seconds
    backoffs = [
        min(base * (scheduler._BACKOFF_MULTIPLIER**n), scheduler._BACKOFF_MAX_SECONDS)
        for n in range(1, 10)
    ]
    # Strictly increasing until it hits the cap.
    for a, b in zip(backoffs, backoffs[1:]):
        assert b >= a
    # Never exceeds the cap, and the cap is actually reached for a
    # long enough failure streak (otherwise the cap constant is dead
    # code and a failing source would back off forever).
    assert all(b <= scheduler._BACKOFF_MAX_SECONDS for b in backoffs)
    assert backoffs[-1] == scheduler._BACKOFF_MAX_SECONDS

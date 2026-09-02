"""SSRF-hardening regression tests for app.reddit_client, found in a
repo-wide audit:

1. None of the module's httpx clients registered ``ssrf_event_hook``
   (the per-hop guard every other fetcher module in this codebase
   uses), except the proxy client, which must NOT be guarded — the
   documented ``REDDIT_HYDRA_URL`` deployment shape is a trusted,
   operator-configured target commonly on a private Docker address
   (see ``.env.example``: ``http://hydra:8080``), which
   ``check_url_safe`` would otherwise reject.
2. ``fetch_thread_comments``/``_fetch_thread_comments_direct`` turned
   a ``thread_url`` into a request path via a naive
   ``str.startswith("https://www.reddit.com")`` prefix strip. A
   ``thread_url`` that didn't happen to start with that exact
   literal (e.g. a different case, a bare host, or anything else)
   fell through unstripped, and httpx treats an absolute URL passed
   as the ``path`` argument as overriding ``base_url`` entirely —
   sending the request to whatever host was embedded in the string.
   ``_thread_rss_path`` replaces the prefix strip with proper host
   validation.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import app.reddit_client as reddit_client
from app.reddit_client import _thread_rss_path

REDDIT_CLIENT = Path(__file__).resolve().parents[1] / "app" / "reddit_client.py"


# ---------------------------------------------------------------------------
# 1. _thread_rss_path
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_thread_rss_path_accepts_www_reddit_com():
    assert _thread_rss_path("https://www.reddit.com/r/python/comments/abc123/title/") == (
        "/r/python/comments/abc123/title/.rss"
    )


@pytest.mark.no_db
def test_thread_rss_path_accepts_bare_reddit_com():
    assert _thread_rss_path("https://reddit.com/r/python/comments/abc123/title") == (
        "/r/python/comments/abc123/title/.rss"
    )


@pytest.mark.no_db
def test_thread_rss_path_rejects_non_reddit_host():
    """The actual security fix: a thread_url on any other host must
    NOT fall through to being used as an (absolute) request path —
    that would let it override the client's base_url and send the
    request to an attacker-controlled host."""
    assert _thread_rss_path("https://evil.example.com/r/python/comments/abc123/") is None


@pytest.mark.no_db
def test_thread_rss_path_rejects_spoofed_lookalike_host():
    """A host that merely contains 'reddit.com' as a substring
    (e.g. userinfo or subdomain tricks) must not pass — only an
    exact reddit.com / www.reddit.com hostname is accepted."""
    assert _thread_rss_path("https://reddit.com.evil.example.com/x") is None
    assert _thread_rss_path("https://evil.example.com/?reddit.com") is None


@pytest.mark.no_db
def test_thread_rss_path_rejects_non_http_scheme():
    assert _thread_rss_path("javascript:alert(1)") is None
    assert _thread_rss_path("file:///etc/passwd") is None


# ---------------------------------------------------------------------------
# 2. fetch_thread_comments / _fetch_thread_comments_direct reject bad input
#    without ever opening a connection
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_clients():
    saved_direct = reddit_client._direct_client
    saved_proxy = reddit_client._proxy_client
    yield
    reddit_client._direct_client = saved_direct
    reddit_client._proxy_client = saved_proxy


@pytest.mark.asyncio
async def test_fetch_thread_comments_rejects_non_reddit_url_without_network(isolated_clients):
    with patch.object(reddit_client, "is_disabled", lambda: False), \
         patch("httpx.AsyncClient.stream") as mock_stream:
        result = await reddit_client.fetch_thread_comments("https://evil.example.com/x")
    assert result is None
    mock_stream.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_thread_comments_direct_rejects_non_reddit_url_without_network(isolated_clients):
    with patch.object(reddit_client, "_try_take_token", new=AsyncMock(return_value=True)), \
         patch("httpx.AsyncClient.stream") as mock_stream:
        result = await reddit_client._fetch_thread_comments_direct("https://evil.example.com/x")
    assert result is None
    mock_stream.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Source-shape checks: which clients are SSRF-guarded
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_direct_client_and_direct_fallback_register_ssrf_hook():
    src = REDDIT_CLIENT.read_text()
    # The direct-mode (base_url="https://www.reddit.com") client
    # constructions must all carry the hook.
    direct_blocks = src.count('base_url="https://www.reddit.com"')
    assert direct_blocks >= 3, "expected at least 3 direct-mode client constructions"
    assert src.count('event_hooks={"request": [ssrf_event_hook]}') >= 3, (
        "expected the SSRF hook on: init_client's _direct_client, "
        "_get_client's direct fallback, and _fetch_thread_comments_direct's "
        "one-off client"
    )


@pytest.mark.no_db
def test_proxy_client_is_deliberately_not_ssrf_guarded():
    """Pin the asymmetry: unlike every other client in this module,
    the Hydra proxy client must NOT register ssrf_event_hook. The
    documented deployment (``REDDIT_HYDRA_URL=http://hydra:8080``,
    a Docker-internal hostname) would otherwise be rejected by
    check_url_safe's private-address policy, breaking proxy mode
    entirely. A future "let's be consistent" edit should not add
    the hook here without addressing that."""
    src = REDDIT_CLIENT.read_text()
    proxy_ctor = src.split("_proxy_client = httpx.AsyncClient(", 1)[1].split(")", 1)[0]
    assert "ssrf_event_hook" not in proxy_ctor

"""OIDC client + PKCE flow.

Uses ``authlib``'s ``AsyncOAuth2Client`` directly (no Starlette session
middleware — we manage our own state cookie). PKCE is mandatory because
no client secret is configured.

Flow:
    /auth/login       → build authorize URL with code_challenge; stash
                         state + verifier in a short-lived signed cookie;
                         302 to the IdP.
    /auth/callback    → unpack the cookie, exchange the code, fetch userinfo
                         (or parse id_token), mint the session cookie, 302 to /.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client

from app.auth.settings import OIDCConfig

logger = logging.getLogger("popping.auth")


class OIDCError(Exception):
    """User-visible OIDC failure (bad config, IdP down, etc.)."""


# ---------------------------------------------------------------------------
# Discovery metadata (cached per process, with TTL)
# ---------------------------------------------------------------------------

# Cached discovery document per issuer. The first ``_discovery`` call
# hits ``<issuer>/.well-known/openid-configuration`` and stashes the
# result here; subsequent calls reuse it. The audit found this cache
# previously had no expiration, so an IdP rotation (key endpoint
# change, JWKS URL change, token endpoint change) would only be
# picked up on a full process restart.
#
# TTL: 1 hour. OIDC discovery is cheap (single GET), so the trade-off
# is "stale metadata for up to an hour after an IdP rotation" vs.
# "discovery hit on every login". An hour is short enough that a
# midnight IdP rollover is recovered by morning, long enough that
# ``_check_convergence`` (per-tick) doesn't accidentally hammer the
# IdP's discovery endpoint.
#
# The cache key is the issuer URL so a single process supporting
# multiple IdPs (not currently used but cheap to support) gets a
# separate entry per issuer.
_METADATA_TTL_SECONDS = 3600
_metadata_cache: dict[str, tuple[float, dict]] = {}


def _metadata_fresh(entry: tuple[float, dict]) -> bool:
    """``True`` if the cached entry's age is under ``_METADATA_TTL_SECONDS``.

    ``time.monotonic`` rather than wall-clock — a wall-clock jump
    (NTP step, daylight-savings, manual clock set) shouldn't
    invalidate a fresh cache nor keep a stale one alive.
    """
    cached_at, _ = entry
    return (time.monotonic() - cached_at) < _METADATA_TTL_SECONDS


def _fetch_discovery_sync(cfg: OIDCConfig) -> dict:
    """Blocking GET to the IdP's discovery document.

    Kept sync so we can run it via ``asyncio.to_thread`` from the
    async wrapper below. We never want a blocking I/O call on the
    event loop — the previous code used ``httpx.Client`` inline
    inside an async route handler, blocking the loop for up to
    10s on a slow IdP and starving every other request in
    flight.
    """
    try:
        with httpx.Client(timeout=10.0) as c:
            resp = c.get(f"{cfg.issuer}/.well-known/openid-configuration")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        raise OIDCError(
            f"could not fetch OIDC discovery document from {cfg.issuer}: {e}"
        ) from e


# Per-issuer lock so two concurrent cache misses (a cold-start
# stampede) don't fire two discovery requests. Held briefly — the
# lock is only around the cache check + ``to_thread`` dispatch, not
# the network roundtrip itself.
_discovery_locks: dict[str, asyncio.Lock] = {}
_discovery_locks_guard = asyncio.Lock()


async def _get_discovery_lock(issuer: str) -> asyncio.Lock:
    async with _discovery_locks_guard:
        lock = _discovery_locks.get(issuer)
        if lock is None:
            lock = asyncio.Lock()
            _discovery_locks[issuer] = lock
        return lock


async def _discovery(cfg: OIDCConfig) -> dict:
    """Return the cached discovery document for ``cfg.issuer``,
    fetching it on a cache miss.

    Async so the underlying httpx.Client doesn't block the event
    loop. The cache itself is a plain dict (single-writer per
    issuer thanks to the lock above)."""
    entry = _metadata_cache.get(cfg.issuer)
    if entry is not None and _metadata_fresh(entry):
        return entry[1]
    lock = await _get_discovery_lock(cfg.issuer)
    async with lock:
        # Re-check after acquiring the lock — another coroutine
        # may have just filled the cache.
        entry = _metadata_cache.get(cfg.issuer)
        if entry is not None and _metadata_fresh(entry):
            return entry[1]
        meta = await asyncio.to_thread(_fetch_discovery_sync, cfg)
        for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if required not in meta:
                raise OIDCError(
                    f"OIDC discovery at {cfg.issuer} is missing {required!r}"
                )
        _metadata_cache[cfg.issuer] = (time.monotonic(), meta)
        logger.info("OIDC discovery loaded from %s", cfg.issuer)
        return meta


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _make_verifier() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# State cookie — short-lived signed blob carrying state + verifier + return_to
# ---------------------------------------------------------------------------

def _state_serializer(cfg: OIDCConfig):
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(
        secret_key=cfg.session_secret,
        salt="popping-oidc-state-v1",
    )


def pack_state(cfg: OIDCConfig, state: str, code_verifier: str, return_to: str) -> str:
    return _state_serializer(cfg).dumps(
        {"state": state, "verifier": code_verifier, "return_to": return_to}
    )


def unpack_state(cfg: OIDCConfig, value: str) -> dict[str, Any]:
    from itsdangerous import BadSignature, SignatureExpired

    try:
        return _state_serializer(cfg).loads(value, max_age=600)  # 10 min
    except SignatureExpired as e:
        raise OIDCError("login flow expired — please try again") from e
    except BadSignature as e:
        raise OIDCError("login state corrupted — please try again") from e


# ---------------------------------------------------------------------------
# Build authorize URL
# ---------------------------------------------------------------------------

async def build_authorize_url(cfg: OIDCConfig, return_to: str = "/") -> tuple[str, str]:
    """Return (authorize_url, state_cookie_value).

    Async because ``_discovery`` issues a network request on cache
    miss and we never want a sync I/O call on the event loop."""
    meta = await _discovery(cfg)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _make_verifier()
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": cfg.scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{meta['authorization_endpoint']}?{urlencode(params)}"
    cookie_value = pack_state(cfg, state=state, code_verifier=verifier, return_to=return_to)
    return url, cookie_value


# ---------------------------------------------------------------------------
# Token exchange + userinfo
# ---------------------------------------------------------------------------

async def exchange_code(
    cfg: OIDCConfig,
    code: str,
    verifier: str,
) -> dict:
    """Exchange the authorization code for tokens and return user claims
    (sub, email, name, ...).

    Goes straight to the ``userinfo`` endpoint rather than parsing the
    ``id_token``. The id_token path requires a nonce roundtrip (which
    we'd have to wire through the state cookie) and varies by IdP — most
    providers include email/name in userinfo even when they don't set a
    nonce in the id_token. If your IdP doesn't expose a userinfo
    endpoint, set ``OIDC_SCOPES=openid email profile`` and switch this
    function to use ``parse_id_token`` with a nonce plumbed through.
    """
    meta = await _discovery(cfg)
    token_endpoint = meta["token_endpoint"]
    userinfo_endpoint = meta.get("userinfo_endpoint")

    async with AsyncOAuth2Client(client_id=cfg.client_id, code_verifier=verifier) as client:
        try:
            token = await client.fetch_token(
                token_endpoint,
                code=code,
                redirect_uri=cfg.redirect_uri,
            )
        except Exception as e:
            raise OIDCError(f"token exchange failed: {e}") from e

        if not userinfo_endpoint:
            raise OIDCError(
                "IdP discovery has no userinfo_endpoint; popping currently "
                "requires userinfo. File an issue if your IdP can't expose it."
            )
        try:
            # fetch_token sets the token on the client, so .get() will
            # send `Authorization: Bearer <access_token>` automatically.
            resp = await client.get(userinfo_endpoint)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise OIDCError(f"userinfo fetch failed: {e}") from e
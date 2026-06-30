"""OIDC routes: /auth/login, /auth/callback, /auth/logout, /auth/me.

Mounted at the root (not under /api) because the browser hits these
directly. When OIDC is disabled, main.py doesn't include this router,
so /auth/* 404s — clean failure mode.

The ``local`` router is mounted as a sub-router below for /auth/local and
/auth/local/availability. Both routers use the same cookie + session
infrastructure, so a logged-in OIDC user and a logged-in local user look
identical downstream.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import local as local_auth
from app.auth.deps import current_user
from app.auth.oidc import OIDCError, build_authorize_url, exchange_code, unpack_state
from app.auth.session import create as session_create, destroy as session_destroy
from app.auth.settings import OIDCConfig, oidc_config
from app.config import settings
from app.db import get_session

logger = logging.getLogger("popping.auth")

router = APIRouter(tags=["auth"])

# Cap on user-controlled string lengths from the IdP. The session row
# is varchar(255) on sub/email/name; an IdP that ships a 1MB name would
# crash the INSERT with a 500. Truncate at the boundary so a hostile /
# buggy IdP can't take the app down.
_OIDC_CLAIM_MAX = 255

# Mount the local-auth routes (POST /auth/local, GET /auth/local/availability)
# on the same auth router. The endpoints they expose are no-ops unless
# LOCAL_AUTH_ENABLED=true, so it's safe to mount unconditionally here.
router.include_router(local_auth.router)

# State cookie name (separate from session cookie).
_STATE_COOKIE = "popping_oidc_state"


def _is_https(cfg: OIDCConfig) -> bool:
    return cfg.public_url.startswith("https://")


def _cookie_attrs(cfg: OIDCConfig) -> dict[str, Any]:
    """Common cookie attributes. Secure flag follows the public URL scheme."""
    return {
        "httponly": True,
        "secure": _is_https(cfg),
        "samesite": "lax",
        "path": "/",
    }


def _safe_return_to(value: str) -> str:
    """Tighten the post-login redirect target.

    Browsers historically treat ``\\`` and ``//`` as scheme-relative
    boundary characters; Starlette's ``URL`` and ``urllib.parse`` also
    treat ``\\`` as ``/`` on some platforms (notably older browsers
    + Windows-native URL parsers). A return_to like ``/\\evil.com`` or
    ``/\\\\evil.com`` can therefore escape the origin. Also reject
    anything with a non-empty netloc (an absolute URL like
    ``https://evil.com``) and any path containing ``:`` before the
    first ``/`` (a scheme-shaped prefix the browser may resolve as
    ``javascript:``-style).

    The result is a same-origin relative path only. Anything else
    falls back to ``/`` so the worst case is "you land on the root",
    not "you got phished".
    """
    if not isinstance(value, str) or not value:
        return "/"
    if not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return "/"
    # Anything past the first ``/``/``\`` is a path — reject control
    # chars and absolute-URL prefixes anywhere in it. ``\\`` is
    # already blocked at the start above, but defense-in-depth on the
    # full string catches ``/foo\\bar`` style payloads too.
    if "\\" in value:
        return "/"
    try:
        parsed = urlparse(value)
    except ValueError:
        return "/"
    if parsed.scheme or parsed.netloc:
        return "/"
    return value


# ---------------------------------------------------------------------------
# /auth/login — kick off the OIDC flow
# ---------------------------------------------------------------------------


@router.get("/auth/login")
async def login(return_to: str = "/") -> Response:
    """Redirect to the IdP. Stashes state+verifier in a short cookie."""
    cfg = oidc_config()
    # Disallow open redirects. ``_safe_return_to`` returns either the
    # user-supplied path (when safe) or ``/`` as a fallback.
    return_to = _safe_return_to(return_to)
    try:
        authorize_url, state_cookie_value = build_authorize_url(cfg, return_to=return_to)
    except OIDCError as e:
        logger.error("OIDC discovery failed: %s", e)
        raise HTTPException(status_code=503, detail=f"OIDC not available: {e}") from e

    resp = RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)
    resp.set_cookie(
        _STATE_COOKIE,
        state_cookie_value,
        max_age=600,            # 10 min
        **_cookie_attrs(cfg),
    )
    return resp


# ---------------------------------------------------------------------------
# /auth/callback — finish the OIDC flow
# ---------------------------------------------------------------------------


@router.get("/auth/callback")
async def callback(
    request: Request,
    code: str,
    state: str,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Exchange the code, mint a session row, set the cookie, redirect."""
    cfg = oidc_config()

    state_cookie = request.cookies.get(_STATE_COOKIE)
    if not state_cookie:
        raise HTTPException(status_code=400, detail="login state cookie missing")

    try:
        st = unpack_state(cfg, state_cookie)
    except OIDCError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Constant-time compare so a guess-the-state timing attack doesn't
    # give the attacker a side-channel on the cookie value. hmac.
    # compare_digest requires equal-length strings; if the lengths
    # differ the call still returns False (and runs in constant time
    # relative to the *shorter* input) — which is exactly the
    # behaviour we want: no early "state is clearly wrong" leak.
    expected = st.get("state") or ""
    if not hmac.compare_digest(state, expected):
        raise HTTPException(status_code=400, detail="login state mismatch")
    verifier = st["verifier"]
    return_to = st.get("return_to") or "/"

    try:
        claims = await exchange_code(cfg, code, verifier)
    except OIDCError as e:
        logger.warning("token exchange failed: %s", e)
        raise HTTPException(status_code=502, detail=f"OIDC exchange failed: {e}") from e

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=502, detail="OIDC claims missing 'sub'")
    # Truncate user-controlled claims at the schema boundary so a
    # hostile / buggy IdP can't take down login with a 500 on a 1MB
    # email field. ``sub`` is the primary key for sessions and must
    # round-trip, so it's validated separately and rejected outright
    # if oversized — that case almost certainly means the IdP is
    # misconfigured and we don't want a truncated sub silently
    # colliding with an existing session row.
    if not isinstance(sub, str) or len(sub) > _OIDC_CLAIM_MAX:
        logger.warning("OIDC 'sub' over %d bytes — rejecting", _OIDC_CLAIM_MAX)
        raise HTTPException(status_code=502, detail="OIDC 'sub' too long")
    sub = sub[:_OIDC_CLAIM_MAX]
    email = claims.get("email")
    if isinstance(email, str):
        email = email[:_OIDC_CLAIM_MAX] or None
    name = (
        claims.get("name")
        or claims.get("preferred_username")
        or claims.get("email")
    )
    if isinstance(name, str):
        name = name[:_OIDC_CLAIM_MAX] or None

    sid = await session_create(
        db,
        cfg,
        sub=sub,
        email=email,
        name=name,
        auth_method="oidc",
    )

    resp = RedirectResponse(return_to, status_code=status.HTTP_302_FOUND)
    resp.set_cookie(
        cfg.cookie_name,
        sid,
        max_age=cfg.session_ttl_seconds,
        **_cookie_attrs(cfg),
    )
    resp.delete_cookie(_STATE_COOKIE, path="/")
    logger.info("OIDC login ok sub=%s", sub)
    return resp


# ---------------------------------------------------------------------------
# /auth/logout — clear the session cookie + delete the row
# ---------------------------------------------------------------------------


@router.post("/auth/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    cfg = oidc_config()
    sid = request.cookies.get(cfg.cookie_name)
    if sid:
        await session_destroy(db, sid)
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.delete_cookie(cfg.cookie_name, path="/")
    return resp


# ---------------------------------------------------------------------------
# /auth/me — current user payload, or 401
# ---------------------------------------------------------------------------


@router.get("/auth/me")
async def me(
    user: dict | None = Depends(current_user),
) -> dict:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not logged in"
        )
    return {
        "sub": user.get("sub"),
        "email": user.get("email"),
        "name": user.get("name"),
        "auth_method": user.get("auth_method"),
    }
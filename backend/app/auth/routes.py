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

import logging
from typing import Any

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


# ---------------------------------------------------------------------------
# /auth/login — kick off the OIDC flow
# ---------------------------------------------------------------------------


@router.get("/auth/login")
async def login(return_to: str = "/") -> Response:
    """Redirect to the IdP. Stashes state+verifier in a short cookie."""
    cfg = oidc_config()
    # Disallow open redirects: only allow relative paths starting with /
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/"
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

    if state != st.get("state"):
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

    email = claims.get("email")
    name = (
        claims.get("name")
        or claims.get("preferred_username")
        or claims.get("email")
    )

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
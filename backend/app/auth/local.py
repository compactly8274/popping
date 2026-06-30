"""Local fallback user.

A hardcoded local account that bypasses OIDC. Useful when the IdP is
down or you're testing the deployment without standing one up. Configure
with ``LOCAL_AUTH_ENABLED=true`` plus a bcrypt-hashed password.

The login route mints the same kind of session row as the OIDC callback,
so downstream code (deps, mutations, /auth/me) doesn't care which path
produced the session — they only see ``auth_method`` in the payload.
"""

from __future__ import annotations

import hmac
import logging
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import create as session_create
from app.auth.settings import OIDCConfig, oidc_config
from app.config import settings
from app.db import get_session

logger = logging.getLogger("popping.auth")

router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


# Bounded so a hostile caller can't pin 256KB of password in a body
# and force bcrypt to chew on it for many seconds. ``max_length`` on
# the schema raises a 422 before we even start hashing.
class LocalLoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


# ---------------------------------------------------------------------------
# Verification (constant-time via bcrypt.checkpw)
# ---------------------------------------------------------------------------


class _LocalAuthError(Exception):
    """Bad credentials; never raise with detail (avoid user enumeration)."""


def _verify_local_credentials(username: str, password: str) -> tuple[str, str, str]:
    """Return (sub, email, name) for the local user, or raise.

    Always runs bcrypt.checkpw against the stored hash — even when the
    username is wrong — to keep the response time constant.
    """
    if not settings.local_auth_enabled:
        raise _LocalAuthError("local auth disabled")

    expected_user = settings.local_user_name
    expected_hash = settings.local_user_password_hash

    # If the local user isn't fully configured, treat every login as a
    # failure rather than 500ing. This is the "I forgot to set the hash"
    # safety net.
    if not expected_user or not expected_hash:
        raise _LocalAuthError("local auth not configured")

    # ``bcrypt.checkpw`` raises ``ValueError`` if the hash is malformed
    # and ``UnicodeEncodeError`` if the password can't be encoded. We
    # catch both as a credential failure — same constant-time behavior
    # from the caller's perspective (no info leak via exception class).
    try:
        pw_bytes = password.encode("utf-8")
        ok = bcrypt.checkpw(pw_bytes, expected_hash.encode("utf-8"))
    except (ValueError, TypeError, UnicodeEncodeError):
        ok = False

    # Constant-time compare on the username so a "guess the local
    # username" timing attack doesn't shorten the brute-force keyspace
    # beyond bcrypt's own constant-time guarantee. ``hmac.
    # compare_digest`` runs in time proportional to the *shorter*
    # input — different-length usernames still take ~equal wall-clock
    # because both paths are bcrypt-bound, which dominates the
    # username compare by orders of magnitude.
    user_match = hmac.compare_digest(username or "", expected_user or "")

    if not ok or not user_match:
        raise _LocalAuthError("invalid credentials")

    sub = f"local:{username}"
    return sub, settings.local_user_email or "", username


# ---------------------------------------------------------------------------
# Cookie helper — duplicated from routes.py rather than imported to keep
# this module self-contained.
# ---------------------------------------------------------------------------


def _cookie_attrs(cfg: OIDCConfig) -> dict:
    return {
        "httponly": True,
        "secure": cfg.public_url.startswith("https://"),
        "samesite": "lax",
        "path": "/",
    }


# ---------------------------------------------------------------------------
# POST /auth/local
# ---------------------------------------------------------------------------


@router.post("/auth/local")
async def login_local(
    body: LocalLoginIn,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Mint a session for the configured local user. Idempotent w.r.t.
    the request — the same body always produces the same response (modulo
    the new session id)."""
    cfg = oidc_config()

    try:
        sub, email, name = _verify_local_credentials(body.username, body.password)
    except _LocalAuthError:
        # Same response regardless of failure mode (don't leak which
        # field was wrong, don't leak whether local auth is enabled).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        ) from None

    sid = await session_create(
        db,
        cfg,
        sub=sub,
        email=email,
        name=name,
        auth_method="local",
    )

    response.set_cookie(
        cfg.cookie_name,
        sid,
        max_age=cfg.session_ttl_seconds,
        **_cookie_attrs(cfg),
    )
    logger.info("local login ok sub=%s", sub)
    return {"sub": sub, "email": email, "name": name}


# ---------------------------------------------------------------------------
# GET /auth/local/availability — does the UI know whether to render the
# local-auth form? Probed at app load (see frontend api.localAuthEnabled).
# ---------------------------------------------------------------------------


@router.get("/auth/local/availability")
async def local_availability() -> dict:
    """Return ``{"enabled": true}`` when local auth is configured.

    Mounted on the auth router only when OIDC is enabled. Returns 200
    regardless of LOCAL_AUTH_ENABLED so the frontend can distinguish
    "auth surface exists" from "local fallback exists".
    """
    return {"enabled": bool(settings.local_auth_enabled)}
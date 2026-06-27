"""FastAPI dependencies for the current user / require-login.

``current_user`` returns the decoded session payload or None. It:
  1. Reads the session cookie and looks up the DB row (DB-backed session).
  2. If the cookie is absent AND the request came from loopback AND
     ``local_auth_bypass`` is on, returns a synthetic 'local-loopback'
     user. (Bypass is checked AFTER the cookie so an authenticated
     loopback user still gets their real identity.)

``require_user`` 401s when ``current_user`` returns None.

Routes opt in by adding ``user: dict = Depends(require_user)`` to their
signature.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import SessionError, decode
from app.auth.settings import OIDCConfig, oidc_config
from app.config import settings
from app.db import get_session

logger = logging.getLogger("popping.auth")


# ---------------------------------------------------------------------------
# Loopback detection
# ---------------------------------------------------------------------------

_LOOPBACK_SYNTHETIC = {
    "sub": "local-loopback",
    "email": "",
    "name": "Local",
    "auth_method": "loopback",
}


def _resolve_client_ip(request: Request) -> Optional[str]:
    """Read X-Forwarded-For (leftmost) when behind a trusted proxy, else
    fall back to the TCP peer. Returns None if neither is parseable."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    return None


def _is_loopback(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return ip.is_loopback  # covers 127.0.0.0/8 and ::1


def _maybe_loopback_user(request: Request) -> Optional[dict]:
    """Return the synthetic loopback user when the bypass is enabled and
    the request came from a loopback IP. None otherwise."""
    if not settings.local_auth_bypass:
        return None
    ip = _resolve_client_ip(request)
    if ip is None or not _is_loopback(ip):
        return None
    return dict(_LOOPBACK_SYNTHETIC)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def current_user(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict | None:
    """Return the session payload for this request, or None if not logged in.

    Cookie lookup is attempted first; loopback bypass is the fallback for
    cookie-less requests when ``local_auth_bypass`` is on.
    """
    cfg = oidc_config()
    raw = request.cookies.get(cfg.cookie_name)
    if raw:
        try:
            return await decode(db, raw)
        except SessionError:
            pass  # fall through to loopback check
    return _maybe_loopback_user(request)


async def require_user(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """FastAPI dependency: 401 when no valid session or loopback bypass."""
    user = await current_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="login required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
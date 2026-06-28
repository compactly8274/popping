"""FastAPI dependencies for the current user / require-login.

``current_user`` returns the decoded session payload or None. It:
  1. Reads the session cookie and looks up the DB row (DB-backed session).
  2. If the cookie is absent AND the request came from a private
     network address (loopback, RFC1918, link-local, IPv6 ULA) AND
     ``local_auth_bypass`` is on, returns a synthetic 'local-bypass'
     user. (Bypass is checked AFTER the cookie so an authenticated
     LAN user still gets their real identity.)

``require_user`` 401s when ``current_user`` returns None.

Routes opt in by adding ``user: dict = Depends(require_user)`` to their
signature.

SECURITY: the bypass IP is taken from the TCP peer only (``request.
client.host``). X-Forwarded-For is intentionally ignored — a client
can set that header to anything it wants, and trusting it would let
a LAN attacker claim a loopback identity.
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
# Local bypass detection
# ---------------------------------------------------------------------------

# Synthetic user for cookie-less requests from a private address when
# ``local_auth_bypass=true``. We use ``sub="local-bypass"`` (rather
# than the old "local-loopback") so the UserBadge can render "Bypass"
# instead of "Loopback" for non-loopback callers.
_BYPASS_SYNTHETIC = {
    "sub": "local-bypass",
    "email": "",
    "name": "Local",
    "auth_method": "bypass",
}

# Networks considered "local" for the bypass. Composed at import time
# so the membership check is just a flat ``in`` against a small list.
# IPv4 first:
#   127.0.0.0/8        — loopback
#   10.0.0.0/8         — RFC1918
#   172.16.0.0/12      — RFC1918
#   192.168.0.0/16     — RFC1918
#   169.254.0.0/16     — link-local
# IPv6:
#   ::1/128            — loopback
#   fe80::/10          — link-local
#   fc00::/7           — ULA (covers fc00::/8 and fd00::/8)
_PRIVATE_NETS: tuple = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
        "fe80::/10",
        "fc00::/7",
    )
)


def _client_ip(request: Request) -> Optional[str]:
    """Return the TCP peer address.

    We deliberately do NOT read ``X-Forwarded-For``: a client can set
    that header to any value it wants, and trusting it would let a LAN
    attacker claim a loopback identity under the bypass. If you ever
    need real-client-IP from a reverse proxy, add explicit proxy
    trust — see config.py ``local_auth_bypass`` for the warning.
    """
    if request.client and request.client.host:
        return request.client.host
    return None


def _is_private_address(ip_str: str) -> bool:
    """True if the address is loopback, RFC1918, link-local, or IPv6 ULA."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in _PRIVATE_NETS)


def _maybe_bypass_user(request: Request) -> Optional[dict]:
    """Return the synthetic bypass user when the bypass is enabled and
    the request came from a private address. None otherwise. Every
    successful grant is logged at INFO for an audit trail."""
    if not settings.local_auth_bypass:
        return None
    ip = _client_ip(request)
    if ip is None or not _is_private_address(ip):
        return None
    logger.info(
        "local-auth-bypass grant: ip=%s method=%s path=%s",
        ip,
        request.method,
        request.url.path,
    )
    return dict(_BYPASS_SYNTHETIC)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def current_user(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict | None:
    """Return the session payload for this request, or None if not logged in.

    Cookie lookup is attempted first; the local bypass is the fallback
    for cookie-less requests when ``local_auth_bypass`` is on.
    """
    cfg = oidc_config()
    raw = request.cookies.get(cfg.cookie_name)
    if raw:
        try:
            return await decode(db, raw)
        except SessionError:
            pass  # fall through to bypass check
    return _maybe_bypass_user(request)


async def require_user(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """FastAPI dependency: 401 when no valid session or local bypass."""
    user = await current_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="login required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
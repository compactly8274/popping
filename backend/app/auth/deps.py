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

# Networks considered "local" for the bypass. Loaded from the
# ``local_bypass_allowed_cidrs`` setting (comma-separated CIDRs) at
# import time. Default is loopback-only; LAN operators opt in
# explicitly by setting ``LOCAL_BYPASS_ALLOWED_CIDRS``.
#
# We deliberately do NOT include RFC1918 / link-local / ULA by
# default — those CIDRs cover Docker bridge networks, k8s pod
# CIDRs, and reverse-proxy peers bound to private interfaces. A
# request from any of those would have been silently auto-granted
# under the old default; see config.py ``local_auth_bypass`` for the
# full threat model.
def _load_bypass_nets() -> tuple:
    out: list = []
    for cidr in (settings.local_bypass_allowed_cidrs or "").split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            out.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError as exc:
            # Misconfiguration should be loud at startup, not on every
            # request. We log once via the module logger and skip.
            logger.warning(
                "local_bypass: ignoring invalid CIDR %r (%s)", cidr, exc,
            )
    return tuple(out)


_BYPASS_NETS: tuple = _load_bypass_nets()


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
    """True if the address matches one of the configured bypass CIDRs.

    The default is loopback-only; the operator can widen this via
    ``LOCAL_BYPASS_ALLOWED_CIDRS``. The old "any RFC1918 / loopback /
    link-local / ULA" hard-coded set was removed because Docker
    bridges, k8s pod CIDRs, and reverse-proxy peers all sit in
    RFC1918 space — a deployment with ``local_auth_bypass=true`` and
    a reverse proxy would have silently granted every public-facing
    request the bypass identity.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in _BYPASS_NETS)


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

    Note: the cookie name comes from ``settings.session_cookie_name``
    directly, not from ``oidc_config()``. Calling ``oidc_config()``
    here would raise on every request when OIDC is disabled, even
    though we only need the cookie name (which has a default in
    settings). The OIDC config is still consulted in the auth/OIDC
    code paths that actually need it.
    """
    raw = request.cookies.get(settings.session_cookie_name)
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
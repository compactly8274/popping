"""DB-backed sessions.

The session cookie carries only an opaque random ID (``sid``). User data
and expiry live in the ``sessions`` table. The cookie value has ~190 bits
of entropy (32 random bytes, url-safe base64) — brute-force is
infeasible, and a stolen cookie is "you" until expiry (same security
model as JWT bearer tokens).

Why DB-backed rather than a signed blob:
  - Survives backend restart (no mass logout).
  - Allows server-side revocation (logout = ``DELETE``).
  - Allows a sliding TTL with a real ``last_used_at`` timestamp.
  - Easier to audit (rows you can ``SELECT`` from).

Cookie attributes are set by the routes that call ``create()``:
  HttpOnly, SameSite=Lax, Path=/, Max-Age=ttl, Secure when public_url is https.
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.settings import OIDCConfig
from app.models import Session as SessionRow


class SessionError(Exception):
    """Raised when a session can't be decoded / is expired / not found."""


def _new_sid() -> str:
    """32 bytes of randomness, url-safe base64 (no padding)."""
    return secrets.token_urlsafe(32)


async def create(
    db: AsyncSession,
    cfg: OIDCConfig,
    *,
    sub: str,
    email: Optional[str],
    name: Optional[str],
    auth_method: str,  # 'oidc' | 'local' | 'bypass'
) -> str:
    """Insert a session row and return the cookie value (the new sid).

    The TTL is computed here (now + cfg.session_ttl_seconds) and stored in
    ``expires_at`` so cleanup is straightforward.
    """
    sid = _new_sid()
    now = dt.datetime.now(dt.timezone.utc)
    row = SessionRow(
        id=sid,
        sub=sub,
        email=email,
        name=name,
        auth_method=auth_method,
        created_at=now,
        last_used_at=now,
        expires_at=now + dt.timedelta(seconds=cfg.session_ttl_seconds),
    )
    db.add(row)
    await db.commit()
    return sid


async def decode(
    db: AsyncSession,
    sid: str,
) -> dict:
    """Return the user payload for ``sid``, or raise SessionError.

    Touches ``last_used_at`` to implement the sliding TTL. The update is
    fire-and-forget — if it fails the session still works this request.
    """
    now = dt.datetime.now(dt.timezone.utc)
    row = await db.get(SessionRow, sid)
    if row is None:
        raise SessionError("session not found")
    if row.expires_at <= now:
        # Best-effort cleanup of the expired row so it doesn't accumulate.
        await db.delete(row)
        await db.commit()
        raise SessionError("session expired")
    # Sliding refresh: bump last_used_at and extend expires_at. We
    # use UPDATE … RETURNING id so the same query that updates also
    # tells us whether the row still exists — without RETURNING, a
    # concurrent purge_expired() (running every session_purge_interval
    # from the scheduler) can DELETE the row between our SELECT and
    # our UPDATE; the UPDATE then matches 0 rows and silently
    # succeeds, leaving us authenticating a request whose backing
    # row no longer exists. RETURNING gives us the invariant.
    result = await db.execute(
        update(SessionRow)
        .where(
            SessionRow.id == sid,
            SessionRow.expires_at > now,
        )
        .values(
            last_used_at=now,
            expires_at=now + dt.timedelta(seconds=_row_ttl(row)),
        )
        .returning(SessionRow.id)
    )
    if result.scalar_one_or_none() is None:
        raise SessionError("session expired")
    return {
        "sub": row.sub,
        "email": row.email or "",
        "name": row.name or "",
        "auth_method": row.auth_method,
    }


def _row_ttl(row: SessionRow) -> int:
    """How much longer the session has. Falls back to a sane default if
    the row was written by an older release without a stored ttl."""
    remaining = (row.expires_at - dt.datetime.now(dt.timezone.utc)).total_seconds()
    return max(int(remaining), 60)


async def destroy(db: AsyncSession, sid: str) -> None:
    """Delete the session row. Idempotent."""
    await db.execute(delete(SessionRow).where(SessionRow.id == sid))
    await db.commit()


async def purge_expired(db: AsyncSession) -> int:
    """Delete all expired rows. Returns the count for logging."""
    now = dt.datetime.now(dt.timezone.utc)
    result = await db.execute(delete(SessionRow).where(SessionRow.expires_at <= now))
    await db.commit()
    return result.rowcount or 0
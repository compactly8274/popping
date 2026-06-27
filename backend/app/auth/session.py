"""Signed-cookie sessions.

``itsdangerous`` URLSafeTimedSerializer with the configured session secret.
Holds the OIDC user payload and an expiry timestamp. No DB lookup on every
request — the cookie is self-validating.

Cookie attributes set by the routes:
  HttpOnly   — JS can't read it (defense against XSS exfiltration)
  Secure     — when public_url is https (set automatically by routes.py)
  SameSite   — Lax; covers the OIDC callback being a state-changing GET
  Path=/     — sent to /api/* and /auth/*
  Max-Age    — ttl
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.auth.settings import OIDCConfig


class SessionError(Exception):
    """Raised when a session cookie can't be decoded / is expired / tampered."""


def _serializer(cfg: OIDCConfig) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        secret_key=cfg.session_secret,
        salt="popping-session-v1",
    )


def encode(cfg: OIDCConfig, payload: dict) -> str:
    """Sign and return the cookie value (string). Caller sets the cookie."""
    ser = _serializer(cfg)
    return ser.dumps(payload)


def decode(cfg: OIDCConfig, cookie_value: str) -> dict:
    """Return the session payload or raise SessionError."""
    ser = _serializer(cfg)
    try:
        return ser.loads(cookie_value, max_age=cfg.session_ttl_seconds)
    except SignatureExpired as e:
        raise SessionError("session expired") from e
    except BadSignature as e:
        raise SessionError("invalid session") from e


def new_payload(
    sub: str,
    email: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    """Build a fresh session payload (expiry set to now+ttl by the caller
    when encoding, since the serializer enforces max_age on decode)."""
    return {
        "sub": sub,
        "email": email or "",
        "name": name or "",
        "iat": int(dt.datetime.now(dt.timezone.utc).timestamp()),
    }


def is_expired(payload: dict, ttl_seconds: int) -> bool:
    """Optional client-side check; the serializer enforces on decode too."""
    iat = payload.get("iat")
    if not isinstance(iat, (int, float)):
        return True
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    return (now - iat) > ttl_seconds
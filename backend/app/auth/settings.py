"""OIDC runtime config.

Reads the OIDC block from the global ``settings``. Raises on import-time
misconfiguration when OIDC is enabled — we want a clear startup failure
rather than 500s from the first request.

The IdP's metadata (authorization_endpoint, token_endpoint, jwks_uri, etc.)
is fetched lazily by ``app.auth.oidc`` because it can take a few seconds
and we don't want to block container startup on a flaky IdP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger("popping.auth")


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    scopes: str
    public_url: str  # absolute, no trailing slash
    session_secret: str
    session_ttl_seconds: int
    cookie_name: str

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}/auth/callback"


def _load() -> OIDCConfig | None:
    """Return the OIDC config or None if OIDC is disabled.

    Called lazily on first request — we don't want startup to fail when
    OIDC is disabled (the default). When enabled, we validate eagerly so
    a missing env var surfaces as a startup error rather than a confusing
    500 at login time.
    """
    if not settings.oidc_enabled:
        return None
    missing = [
        name
        for name, value in (
            ("OIDC_ISSUER", settings.oidc_issuer),
            ("OIDC_CLIENT_ID", settings.oidc_client_id),
            ("PUBLIC_URL", settings.public_url),
            ("SESSION_SECRET", settings.session_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "OIDC is enabled but the following env vars are unset: "
            + ", ".join(missing)
            + ". Set them in .env (see README → OIDC)."
        )
    cfg = OIDCConfig(
        issuer=settings.oidc_issuer.rstrip("/"),
        client_id=settings.oidc_client_id,
        scopes=settings.oidc_scopes,
        public_url=settings.public_url.rstrip("/"),
        session_secret=settings.session_secret,
        session_ttl_seconds=settings.session_ttl_seconds,
        cookie_name=settings.session_cookie_name,
    )
    logger.info("OIDC enabled: issuer=%s client_id=%s", cfg.issuer, cfg.client_id)
    return cfg


_oidc_config: OIDCConfig | None = None


def oidc_config() -> OIDCConfig:
    """Module-level accessor. Raises if OIDC is disabled.

    The router mounts only when OIDC is enabled, so this is only ever
    called from inside the OIDC code paths.
    """
    global _oidc_config
    if _oidc_config is None:
        _oidc_config = _load()
    if _oidc_config is None:  # pragma: no cover — caller misuse
        raise RuntimeError("OIDC is not enabled")
    return _oidc_config
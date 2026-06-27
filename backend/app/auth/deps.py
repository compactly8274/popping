"""FastAPI dependencies for the current user / require-login.

``current_user`` returns the decoded session payload or None.
``require_user`` is the dependency that 401s when not logged in.

Routes opt in by adding ``user: dict = Depends(require_user)`` to their
signature. When OIDC is disabled, ``current_user`` will raise at request
time — but it's only ever called from inside the /auth/* router, which is
mounted only when OIDC is enabled.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.auth.session import SessionError, decode
from app.auth.settings import oidc_config


def current_user(request: Request) -> dict | None:
    """Return the session payload for this request, or None if not logged in
    (no cookie, expired, or tampered). Never raises."""
    cfg = oidc_config()
    raw = request.cookies.get(cfg.cookie_name)
    if not raw:
        return None
    try:
        return decode(cfg, raw)
    except SessionError:
        return None


def require_user(request: Request) -> dict:
    """FastAPI dependency: 401 when no valid session, else the payload."""
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="login required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
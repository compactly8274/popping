"""Tests for app.auth.local — the hardcoded local-fallback login.

Covers the fix for a bug found in a repo-wide audit: ``login_local``
called ``bcrypt.checkpw`` (via ``_verify_local_credentials``) directly
inside the async route handler. ``bcrypt.checkpw`` is deliberately
slow (its cost factor is the whole point) and purely synchronous, so
calling it inline blocks this single-process event loop for its full
duration — stalling the scheduler's ingest ticks and every other
in-flight request, not just the login path. The fix wraps the call in
``asyncio.to_thread``, mirroring the fix already applied to the OIDC
discovery fetch (``app.auth.oidc._fetch_discovery_sync``).
"""
from __future__ import annotations

from pathlib import Path

import bcrypt
import pytest

from app.auth.local import _LocalAuthError, _verify_local_credentials
from app.config import settings

pytestmark = pytest.mark.no_db

_PASSWORD = "correct-horse-battery-staple"
# Low cost factor (default is 12) so this test suite doesn't pay
# bcrypt's real-world cost budget on every run — the hashing
# algorithm under test is unaffected by the rounds count.
_HASH = bcrypt.hashpw(_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture
def local_auth_configured(monkeypatch):
    monkeypatch.setattr(settings, "local_auth_enabled", True)
    monkeypatch.setattr(settings, "local_user_name", "phil")
    monkeypatch.setattr(settings, "local_user_password_hash", _HASH)
    monkeypatch.setattr(settings, "local_user_email", "phil@example.com")


def test_verify_local_credentials_accepts_correct_password(local_auth_configured):
    sub, email, name = _verify_local_credentials("phil", _PASSWORD)
    assert sub == "local:phil"
    assert email == "phil@example.com"
    assert name == "phil"


def test_verify_local_credentials_rejects_wrong_password(local_auth_configured):
    with pytest.raises(_LocalAuthError):
        _verify_local_credentials("phil", "wrong-password")


def test_verify_local_credentials_rejects_wrong_username(local_auth_configured):
    with pytest.raises(_LocalAuthError):
        _verify_local_credentials("not-phil", _PASSWORD)


def test_verify_local_credentials_rejects_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "local_auth_enabled", False)
    with pytest.raises(_LocalAuthError):
        _verify_local_credentials("phil", _PASSWORD)


def test_verify_local_credentials_rejects_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "local_auth_enabled", True)
    monkeypatch.setattr(settings, "local_user_name", "")
    monkeypatch.setattr(settings, "local_user_password_hash", "")
    with pytest.raises(_LocalAuthError):
        _verify_local_credentials("phil", _PASSWORD)


# --- login_local must not block the event loop ----------------------------


def _login_local_body():
    path = Path(__file__).resolve().parents[1] / "app" / "auth" / "local.py"
    src = path.read_text()
    start = src.index("async def login_local")
    end = src.index("\n\n\n", start)
    return src[start:end]


def test_login_local_route_awaits_verify_via_to_thread():
    """Source-shape pin: the route must dispatch the blocking
    bcrypt-backed verification through ``asyncio.to_thread`` rather
    than calling it inline. Without this, every login attempt stalls
    the whole process for the duration of one bcrypt hash check."""
    body = _login_local_body()
    assert "asyncio.to_thread(" in body, (
        "login_local must call _verify_local_credentials via "
        "asyncio.to_thread, not directly, to avoid blocking the "
        "event loop on bcrypt's deliberately-slow hash check."
    )
    assert "_verify_local_credentials" in body.split("asyncio.to_thread(", 1)[1], (
        "asyncio.to_thread must wrap _verify_local_credentials specifically."
    )

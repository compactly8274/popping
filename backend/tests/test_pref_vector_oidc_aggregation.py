"""Regression tests for a bug found in a repo-wide audit:
``scheduler._resolve_aggregation_user_ids`` unconditionally returned
the fixed ``_AGGREGATION_USER_IDS_ALL`` tuple (``anonymous``,
``local-bypass``, ``default``) regardless of OIDC state, even though
its own docstring already described the intended OIDC-on behavior:
"if there's a resolved OIDC user, scope to their sub". In a real
OIDC-enabled deployment, a logged-in user's interactions (recorded
under their real ``sub``) were silently excluded from every
preference-vector recompute — ``/api/foryou`` personalization was a
permanent no-op for every actual OIDC user.

``UserProfile`` is a genuine single-row table (see its
``uq_user_profiles_single_row`` constraint) — this is a
single-operator app, not multi-tenant, so the fix doesn't give each
OIDC user their own vector; it makes the one shared vector actually
include the real OIDC sub(s) that show up in the interactions table
instead of silently dropping them.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models import Interaction
from app.scheduler import _AGGREGATION_USER_IDS_ALL, _resolve_aggregation_user_ids
from factories import make_entry, make_source


async def test_oidc_off_returns_fixed_tuple(db_session, monkeypatch):
    monkeypatch.setattr(settings, "oidc_enabled", False)
    assert await _resolve_aggregation_user_ids(db_session) == _AGGREGATION_USER_IDS_ALL


async def test_oidc_on_includes_real_oidc_sub(db_session, monkeypatch):
    monkeypatch.setattr(settings, "oidc_enabled", True)
    source = await make_source(db_session, "oidc_agg_source")
    entry = await make_entry(db_session, source, "An entry")
    db_session.add(Interaction(entry_id=entry.id, user_id="google-oauth2|123", type="click"))
    await db_session.commit()

    user_ids = await _resolve_aggregation_user_ids(db_session)

    assert "google-oauth2|123" in user_ids
    assert "local-bypass" in user_ids


async def test_oidc_on_excludes_anonymous_and_default(db_session, monkeypatch):
    """The soft-auth placeholder ids must never be folded into the
    one account's vector — that would blend an unauthenticated
    caller's taste into the single OIDC user's personalization."""
    monkeypatch.setattr(settings, "oidc_enabled", True)
    source = await make_source(db_session, "oidc_agg_source_2")
    entry = await make_entry(db_session, source, "Another entry")
    db_session.add_all([
        Interaction(entry_id=entry.id, user_id="anonymous", type="click"),
        Interaction(entry_id=entry.id, user_id="default", type="click"),
        Interaction(entry_id=entry.id, user_id="google-oauth2|456", type="click"),
    ])
    await db_session.commit()

    user_ids = await _resolve_aggregation_user_ids(db_session)

    assert "anonymous" not in user_ids
    assert "default" not in user_ids
    assert "google-oauth2|456" in user_ids


async def test_oidc_on_no_real_users_yet_still_includes_local_bypass(db_session, monkeypatch):
    """Before any real OIDC login has recorded an interaction, the
    operator's own LAN-bypass traffic should still count — otherwise
    personalization is a no-op on day one of an OIDC-enabled install
    with no interactions recorded at all yet."""
    monkeypatch.setattr(settings, "oidc_enabled", True)
    user_ids = await _resolve_aggregation_user_ids(db_session)
    assert user_ids == ("local-bypass",)

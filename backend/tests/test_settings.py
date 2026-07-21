"""Tests for the /api/settings routes, focused on the API-key fields
added so a user can configure an LLM provider key from the Settings UI
instead of only via .env.

The core contract under test: ``GET /api/settings`` must never echo a
key's value back to the client, only whether one is set. The
null/empty/value update convention (already covered implicitly by
existing manual testing of provider/model fields) is exercised here
specifically for the four ``*_api_key`` fields since they're new.

Cache note: ``runtime_settings`` keeps a module-level in-process cache
that ``_clean_tables`` (which only TRUNCATEs tables) doesn't clear
between tests, so each test invalidates it explicitly to avoid state
leaking from a previous test's writes.
"""

from __future__ import annotations

import pytest

from app import runtime_settings
from app.runtime_settings import _SECRET_KEYS, _SETTINGS_FIELDS


@pytest.fixture(autouse=True)
def _reset_runtime_settings_cache():
    runtime_settings.invalidate_all()
    yield
    runtime_settings.invalidate_all()


async def test_get_settings_defaults_when_nothing_configured(app_client):
    resp = await app_client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["anthropic_api_key_set"] is False
    assert body["openai_api_key_set"] is False
    assert body["groq_api_key_set"] is False
    assert body["ollama_cloud_api_key_set"] is False


async def test_put_llm_sets_api_key_and_get_reflects_boolean(app_client):
    resp = await app_client.put("/api/settings/llm", json={"groq_api_key": "gsk_secret_value"})
    assert resp.status_code == 200
    assert resp.json()["groq_api_key_set"] is True

    resp = await app_client.get("/api/settings")
    assert resp.json()["groq_api_key_set"] is True
    # The other three stay unset — setting one key doesn't bleed into another.
    assert resp.json()["anthropic_api_key_set"] is False


async def test_put_llm_response_never_contains_raw_key_value(app_client):
    resp = await app_client.put("/api/settings/llm", json={"groq_api_key": "gsk_super_secret_value"})
    assert "gsk_super_secret_value" not in resp.text

    resp = await app_client.get("/api/settings")
    assert "gsk_super_secret_value" not in resp.text


async def test_put_llm_clear_api_key_resets_to_unset(app_client):
    await app_client.put("/api/settings/llm", json={"openai_api_key": "sk-something"})
    resp = await app_client.get("/api/settings")
    assert resp.json()["openai_api_key_set"] is True

    resp = await app_client.put("/api/settings/llm", json={"openai_api_key": ""})
    assert resp.json()["openai_api_key_set"] is False

    resp = await app_client.get("/api/settings")
    assert resp.json()["openai_api_key_set"] is False


async def test_put_llm_omitted_key_field_leaves_existing_value_untouched(app_client):
    await app_client.put("/api/settings/llm", json={"groq_api_key": "gsk_keep_me"})

    # A follow-up PUT that only touches the model field (key omitted,
    # i.e. null) must not clear the previously-set key.
    resp = await app_client.put("/api/settings/llm", json={"model_brief": "llama3.1:8b"})
    assert resp.status_code == 200
    assert resp.json()["groq_api_key_set"] is True

    resp = await app_client.get("/api/settings")
    assert resp.json()["groq_api_key_set"] is True


def test_secret_keys_are_a_subset_of_settings_fields():
    # seed_from_env iterates _SETTINGS_FIELDS and skips anything in
    # _SECRET_KEYS — a typo'd/removed entry here would silently start
    # seeding a secret into the DB from env, so pin the invariant.
    assert _SECRET_KEYS <= set(_SETTINGS_FIELDS)
    assert _SECRET_KEYS == {
        "llm.anthropic_api_key",
        "llm.openai_api_key",
        "llm.groq_api_key",
        "llm.ollama_cloud_api_key",
    }

"""Tests for the LLM provider chain selection in app.llm.

The chain (Anthropic → OpenAI → Groq → Ollama Cloud → local
Ollama) is built by ``Router.providers_for(task)``. The
local-Ollama fallback is the catch-all but it requires the
default ``http://host.docker.internal:11434`` to resolve —
which is a Mac/Windows Docker Desktop convenience, not a
Linux + custom-network reality. ``settings.ollama_disable_local_fallback``
lets operators opt out so they don't pay a per-call transport
error in the logs.

Verified shape: when the setting is true, the chain ends at
Ollama Cloud (or earlier — Anthropic / OpenAI / Groq are
skipped when no auth is configured). The local Ollama is
never appended.
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import patch

os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_USER", "x")
os.environ.setdefault("POSTGRES_PASSWORD", "x")
os.environ.setdefault("POSTGRES_DB", "x")
os.environ.setdefault("EMBEDDING_ENABLED", "false")
os.environ.setdefault("ASSETS_DIR", tempfile.mkdtemp(prefix="smoke-"))

sys.path.insert(0, "/tmp/popping-review/backend")

import pytest

from app.llm import Router
from app.llm.ollama import OllamaProvider
from app.llm.ollama_cloud import OllamaCloudProvider


@pytest.fixture
def router() -> Router:
    """A fresh Router with the singleton reset, so each test
    gets its own providers_for() result."""
    return Router()


class TestProvidersForLocalFallback:
    """The local-Ollama provider is appended to the chain
    only when ``settings.ollama_disable_local_fallback`` is
    False. With cloud auth configured (the common
    single-host LAN case), the cloud provider is in the
    chain regardless; the local one is gated by the setting."""

    def test_local_fallback_appended_by_default(self, router: Router) -> None:
        # No cloud auth, no other auth — chain should still
        # include local Ollama (it's the unconditional fallback).
        with patch("app.llm.runtime_settings.snapshot_sync", return_value={}):
            providers = router.providers_for("brief")
        names = [p.name for p in providers]
        assert "ollama" in names

    def test_local_fallback_skipped_when_disabled(self, router: Router) -> None:
        # Same setup, but the operator opted out.
        # The LLM module does ``from app.config import settings``
        # at module load, so the patch target is the imported
        # name in app.llm — not app.config.settings.
        with patch("app.llm.runtime_settings.snapshot_sync", return_value={}):
            with patch("app.llm.settings") as mock_settings:
                mock_settings.ollama_disable_local_fallback = True
                providers = router.providers_for("brief")
        names = [p.name for p in providers]
        assert "ollama" not in names

    def test_local_fallback_present_when_enabled_explicitly(
        self, router: Router
    ) -> None:
        # Explicit opt-in (the default). Sanity check that
        # ``False`` doesn't accidentally also disable.
        with patch("app.llm.runtime_settings.snapshot_sync", return_value={}):
            with patch("app.llm.settings") as mock_settings:
                mock_settings.ollama_disable_local_fallback = False
                providers = router.providers_for("brief")
        names = [p.name for p in providers]
        assert "ollama" in names

    def test_cloud_provider_still_present_when_local_disabled(
        self, router: Router
    ) -> None:
        # The setting only drops the local fallback, not the
        # cloud one. (Both are "ollama" brand; the chain
        # distinguishes them by class.)
        #
        # The API key comes from ``runtime_settings.snapshot_sync()``
        # under the key ``llm.ollama_cloud_api_key`` — the model
        # name comes from ``settings.ollama_cloud_model_brief``
        # via ``_env_model_for``. We mock both, then verify the
        # resulting chain.
        snap = {"llm.ollama_cloud_api_key": "test-key"}
        with patch("app.llm.runtime_settings.snapshot_sync", return_value=snap):
            with patch("app.llm.settings") as mock_settings:
                mock_settings.ollama_disable_local_fallback = True
                mock_settings.ollama_cloud_api_key = "test-key"
                mock_settings.ollama_cloud_model_brief = "glm-5.2:cloud"
                mock_settings.ollama_model_brief = "llama3.1:8b"
                providers = router.providers_for("brief")
        # No local fallback, but the cloud one is there.
        assert all(
            not isinstance(p, OllamaProvider) for p in providers
        ), f"local Ollama should be excluded: {[p.name for p in providers]}"
        cloud = [
            p for p in providers
            if isinstance(p, OllamaCloudProvider)
        ]
        assert len(cloud) == 1
        assert cloud[0].name == "ollama_cloud"

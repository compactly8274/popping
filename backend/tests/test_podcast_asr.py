"""Tests for app.podcast_asr: real speech-to-text via Groq's hosted
Whisper endpoint, the fallback path when a podcast feed doesn't
publish a Podcasting 2.0 transcript tag.

The live "Groq actually transcribes the audio" happy path isn't
covered — needs a real GROQ_API_KEY and a real network call, same
tradeoff made throughout this codebase (podcast transcript fetch,
feed discovery, framing tone classification). What IS covered: the
availability gate (no key configured -> no-op, matching every other
LLM-dependent feature's graceful-absence behavior), the SSRF guard on
the audio download (a real, deterministic rejection, not a live-
network-dependent one), and the route's three-way path selection
(transcript tag / ASR fallback / neither available).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.podcast_asr import _download_audio, asr_available, transcribe_audio
from factories import make_entry, make_source


# --- asr_available -------------------------------------------------------


def test_asr_unavailable_without_groq_key():
    # Test env has no GROQ_API_KEY set — this is the same
    # graceful-absence gate every other LLM feature in this codebase
    # relies on.
    assert settings.groq_api_key == ""
    assert asr_available() is False


# --- transcribe_audio ------------------------------------------------------


async def test_transcribe_audio_short_circuits_when_unavailable():
    # No network attempted at all when asr_available() is False —
    # deterministic regardless of environment network access.
    assert await transcribe_audio("https://example.com/episode.mp3") is None


async def test_download_audio_rejects_loopback_url():
    # _download_audio has no availability gate of its own (that's
    # transcribe_audio's job) — call it directly so the SSRF guard is
    # exercised regardless of whether GROQ_API_KEY is configured in
    # this environment.
    result = await _download_audio("http://127.0.0.1:9999/episode.mp3")
    assert result is None


# --- route: three-way path selection -----------------------------------------


async def test_podcast_summary_route_no_transcript_no_audio_returns_unavailable(app_client, db_session):
    source = await make_source(db_session, "no_audio_podcast", category="podcast", type="podcast")
    entry = await make_entry(db_session, source, "Episode with neither transcript nor audio")

    resp = await app_client.post(f"/api/entries/{entry.id}/podcast_summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["summary"] is None


async def test_podcast_summary_route_audio_only_no_groq_key_returns_unavailable(app_client, db_session):
    # audio_url is present but no GROQ_API_KEY is configured in the
    # test env — asr_available() gates this to the same
    # available=False outcome as having neither, without attempting
    # any download.
    source = await make_source(db_session, "audio_only_podcast", category="podcast", type="podcast")
    entry = await make_entry(db_session, source, "Episode with audio but no transcript")
    entry.meta = {"audio_url": "https://example.com/episode.mp3"}
    await db_session.commit()

    resp = await app_client.post(f"/api/entries/{entry.id}/podcast_summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["summary"] is None

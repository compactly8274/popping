"""Real speech-to-text for podcast episodes, via Groq's hosted Whisper
endpoint — the fallback when a feed doesn't publish a Podcasting 2.0
``<podcast:transcript>`` tag (see ``app.podcast_transcript`` for that
cheaper, transcript-reuse path, which is always tried first).

Why Groq specifically, and why no local/self-hosted model: Groq's
Whisper Large v3 Turbo is priced at ~$0.04/hour of audio (a 90-minute
episode costs about $0.06) and transcribes at roughly 200x realtime —
an hour of audio comes back in under 20 seconds. It's also already a
configured provider in this codebase's LLM router (``app.llm.groq`` —
same ``GROQ_API_KEY``), so this reuses existing credentials rather
than standing up a new service or a local ASR model. Running Whisper
locally (faster-whisper / whisper.cpp on CPU) would be free but adds a
large model download, a slow CPU transcription path (many times
realtime rather than a fraction of it), and a new dependency — none of
which beats "reuse the key you already have" on cost, speed, or
complexity. If ``GROQ_API_KEY`` isn't set, this feature is simply
unavailable — same graceful-absence behavior as every other
LLM-dependent feature in this codebase.
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.url_safety import check_url_safe

logger = logging.getLogger("popping.podcast_asr")

# Groq's free tier caps uploads at 25MB; paid ("dev") tier goes to
# 100MB. We don't know which tier the configured key is on, so we
# target the conservative free-tier ceiling — a 25MB MP3 at a typical
# ~128kbps podcast bitrate is roughly 25-30 minutes of audio. Longer
# episodes get truncated to this many bytes rather than skipped
# entirely: a partial transcript (the episode's first ~25-30 minutes)
# is still useful summarization input, same philosophy as
# app.podcast_transcript's character-budget truncation on the text
# side.
_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0)
_TRANSCRIBE_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=120.0)
_GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_GROQ_MODEL = "whisper-large-v3-turbo"


def asr_available() -> bool:
    """True if a Groq API key is configured — the only gate on this
    feature. Checked by the route layer before attempting anything so
    a missing key degrades to "not available" (same as a missing
    transcript), not an error."""
    return bool(settings.groq_api_key)


async def _download_audio(url: str) -> tuple[bytes, str] | None:
    """Fetch ``url`` and return ``(bytes, content_type)``, truncated
    to ``_MAX_AUDIO_BYTES``. None on any failure (network, SSRF
    rejection, empty body). Same per-hop SSRF guard as
    ``app.podcast_transcript.fetch_transcript_text`` — entry-time
    check, then a second check on the final URL after redirects."""
    safe, reason = check_url_safe(url)
    if not safe:
        logger.warning("podcast_asr: %s rejected by URL safety check (%s)", url, reason)
        return None
    try:
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True, max_redirects=5) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    logger.warning("podcast_asr: %s -> HTTP %s", url, resp.status_code)
                    return None
                final_url = str(resp.url)
                if final_url != url:
                    final_safe, final_reason = check_url_safe(final_url)
                    if not final_safe:
                        logger.warning(
                            "podcast_asr: %s redirected to denied host %s (%s)",
                            url, final_url, final_reason,
                        )
                        return None
                content_type = resp.headers.get("content-type", "audio/mpeg").split(";")[0].strip()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > _MAX_AUDIO_BYTES:
                        logger.info(
                            "podcast_asr: %s exceeded %d bytes — truncating",
                            url, _MAX_AUDIO_BYTES,
                        )
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
    except httpx.HTTPError as exc:
        logger.warning("podcast_asr: %s fetch failed: %s", url, exc)
        return None
    if not body:
        return None
    return body, content_type


async def transcribe_audio(audio_url: str) -> str | None:
    """Download ``audio_url`` and transcribe it via Groq's hosted
    Whisper endpoint. Returns None if ASR isn't configured
    (``asr_available()`` is False), the download fails, or the
    transcription call fails — callers treat this the same as "no
    transcript available", not an error."""
    if not asr_available():
        return None
    downloaded = await _download_audio(audio_url)
    if downloaded is None:
        return None
    audio_bytes, content_type = downloaded

    headers = {"authorization": f"Bearer {settings.groq_api_key}"}
    files = {"file": ("episode.audio", audio_bytes, content_type)}
    data = {"model": _GROQ_MODEL, "response_format": "text"}
    try:
        async with httpx.AsyncClient(timeout=_TRANSCRIBE_TIMEOUT) as client:
            resp = await client.post(_GROQ_TRANSCRIPTION_URL, headers=headers, files=files, data=data)
    except httpx.HTTPError as exc:
        logger.warning("podcast_asr: groq transcription request failed: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning(
            "podcast_asr: groq returned %s: %s", resp.status_code, resp.text[:200],
        )
        return None
    text = resp.text.strip()
    return text or None

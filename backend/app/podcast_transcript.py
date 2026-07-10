"""Podcast transcript fetch + LLM summarization.

Feeds that publish a Podcasting 2.0 ``<podcast:transcript>`` tag
(extracted at ingest time — see ``app.sources.rss``) let us skip
speech-to-text entirely: fetch the transcript the podcast host
already produced, and summarize it with the same LLM provider the
Brief generator uses. No audio processing, no per-minute transcription
cost — this only works for feeds that opt into the tag, which is why
it's presented in the UI as "Summarize episode" rather than always
available (see ``routes/entries.py``'s handling of a missing
transcript).

Deliberately NOT doing real speech-to-text (Whisper / AssemblyAI /
Deepgram) here — that's a separate, paid-per-minute feature with its
own cost model, held off pending user testing of this cheaper path.
"""

from __future__ import annotations

import logging
import re

import httpx

from app.llm import ProviderError, router
from app.url_safety import check_url_safe

logger = logging.getLogger("popping.podcast_transcript")

# Transcripts are text, not media — generous but bounded. 2 MB
# comfortably covers a multi-hour episode's worth of dialogue even
# in the more verbose caption formats (VTT/SRT repeat each line
# twice: once as a cue, once in the timing block).
_MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
_FETCH_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
# WebVTT/SRT cue lines: "00:00:01.000 --> 00:00:04.000" (VTT) or
# "00:00:01,000 --> 00:00:04,000" (SRT, comma instead of period).
_TIMING_CUE_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}.*$"
)
_SRT_SEQUENCE_RE = re.compile(r"^\d+$")


async def fetch_transcript_text(url: str, content_type: str) -> str | None:
    """Fetch ``url`` and return its plain-text content, or None on
    any failure (network, SSRF rejection, empty body). ``content_type``
    is the ``<podcast:transcript type="...">`` attribute value from
    the feed — it decides how the response body gets turned into
    plain text.

    SSRF: same per-hop guard as ``app.sources.rss.fetch_rss`` and
    ``app.assets._download`` — entry-time check, then a second check
    on the final URL after following redirects, since the entry
    check alone misses a ``public.example.com -> 127.0.0.1`` hop.
    """
    safe, reason = check_url_safe(url)
    if not safe:
        logger.warning("podcast_transcript: %s rejected by URL safety check (%s)", url, reason)
        return None
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True, max_redirects=5) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    logger.warning("podcast_transcript: %s -> HTTP %s", url, resp.status_code)
                    return None
                final_url = str(resp.url)
                if final_url != url:
                    final_safe, final_reason = check_url_safe(final_url)
                    if not final_safe:
                        logger.warning(
                            "podcast_transcript: %s redirected to denied host %s (%s)",
                            url, final_url, final_reason,
                        )
                        return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(chunk_size=32 * 1024):
                    total += len(chunk)
                    if total > _MAX_TRANSCRIPT_BYTES:
                        logger.warning(
                            "podcast_transcript: %s exceeded %d bytes — truncating",
                            url, _MAX_TRANSCRIPT_BYTES,
                        )
                        break
                    chunks.append(chunk)
                body = b"".join(chunks).decode("utf-8", errors="replace")
    except httpx.HTTPError as exc:
        logger.warning("podcast_transcript: %s fetch failed: %s", url, exc)
        return None
    if not body.strip():
        return None
    return _to_plain_text(body, content_type)


def _to_plain_text(body: str, content_type: str) -> str:
    """Convert a transcript body to plain text based on its declared
    type. Falls back to returning the body as-is for unrecognized
    types — a transcript of unknown shape is still better summarization
    input than nothing, and the LLM is reasonably tolerant of stray
    markup in its prompt."""
    ct = (content_type or "").lower()
    if "json" in ct:
        return _plain_text_from_json(body)
    if "html" in ct:
        return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", body)).strip()
    if "vtt" in ct or "srt" in ct:
        return _plain_text_from_captions(body)
    return body.strip()


def _plain_text_from_json(body: str) -> str:
    """Podcast Index's transcript JSON schema is ``{"segments": [{"body":
    "...", "speaker": "...", ...}, ...]}``. Some hosts ship a flatter
    ``{"text": "..."}`` shape. Handle both; fall back to the raw body
    if neither matches (better than silently returning nothing)."""
    import json

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body.strip()
    if isinstance(data, dict):
        segments = data.get("segments")
        if isinstance(segments, list) and segments:
            lines = []
            for seg in segments:
                if isinstance(seg, dict):
                    text = seg.get("body") or seg.get("text")
                    if text:
                        speaker = seg.get("speaker")
                        lines.append(f"{speaker}: {text}" if speaker else str(text))
            if lines:
                return "\n".join(lines)
        text = data.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return body.strip()


def _plain_text_from_captions(body: str) -> str:
    """Strip WEBVTT headers, SRT sequence numbers, and timing-cue
    lines from a VTT/SRT transcript, leaving just the spoken text."""
    lines_out: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("WEBVTT"):
            continue
        if _TIMING_CUE_RE.match(line):
            continue
        if _SRT_SEQUENCE_RE.match(line):
            continue
        lines_out.append(line)
    return " ".join(lines_out)


# Transcript text budget fed to the LLM. A 1-hour conversational
# podcast runs roughly 8-9k words (~45-50k characters); this cap
# keeps the prompt well within every configured provider's context
# window without needing a map-reduce chunking pass, at the cost of
# the summary only reflecting the first ~40 minutes of a longer
# episode. Simple beats thorough here — chunking is a reasonable
# follow-up if truncation turns out to matter in practice.
_TRANSCRIPT_CHAR_BUDGET = 40_000
_SUMMARY_MAX_TOKENS = 500


def _build_prompt(episode_title: str, transcript_text: str) -> str:
    truncated = transcript_text[:_TRANSCRIPT_CHAR_BUDGET]
    return (
        "Write a medium-length summary (4-6 sentences, plain prose, "
        "no headers or bullet lists) of the following podcast episode "
        "transcript. Cover the main topics discussed and any notable "
        "conclusions or takeaways. Do not mention that this is a "
        "transcript or that you are summarizing — write as if "
        "describing the episode to someone deciding whether to "
        f"listen.\n\nEpisode: {episode_title}\n\nTranscript:\n{truncated}"
    )


async def summarize_transcript(episode_title: str, transcript_text: str) -> str | None:
    """Summarize ``transcript_text`` via the configured LLM provider
    (same fallback chain the Brief generator uses). Returns None if
    no provider is configured or every configured provider fails."""
    providers = router.providers_for("brief")
    if not providers:
        logger.info("podcast_transcript: no LLM provider configured — skipping summary")
        return None
    prompt = _build_prompt(episode_title, transcript_text)
    for candidate in providers:
        try:
            content = await candidate.complete(prompt, max_tokens=_SUMMARY_MAX_TOKENS)
        except ProviderError as exc:
            logger.warning(
                "podcast_transcript: LLM call failed on %s: %s — trying next provider",
                candidate.name, exc,
            )
            continue
        content = (content or "").strip()
        if content:
            return content
    logger.warning("podcast_transcript: all configured LLM providers failed")
    return None

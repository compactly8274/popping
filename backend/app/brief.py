"""The Brief — a daily AI-generated digest of the top entries.

One LLM call per Brief. Output is a small structured text (one line
+ 3-5 highlights + 3 watch items) that's persisted to ``briefs`` and
optionally pushed to the notification backend.

Three trigger paths all call ``BriefGenerator.generate(tone=...)``:

  1. Scheduled  — daily cron at ``BRIEF_SCHEDULE_HOUR`` (default 08:00 UTC).
  2. Manual     — ``POST /api/brief/generate``.
  3. Convergence — periodic scheduler job. When a slug appears in N+
                   sources within the window, generate a ``tone="alert"``
                   Brief and notify.

Brief generation is best-effort. If no LLM provider is configured the
generator logs and skips; the dashboard keeps working with whatever
Brief is already in the table.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ProviderError, router
from app.models import Brief, Entry, Source
from app.notify import Notifier
from app import runtime_settings

logger = logging.getLogger("popping.brief")


# How many top entries the digest covers. Large enough to give the LLM
# real choice; small enough that the prompt stays under a few KB.
_DIGEST_LIMIT = 25
# Brief output budget. 900 tokens is plenty for a 5-bullet digest + a
# one-sentence summary. Pushover's body limit is ~4 KB which is ~1k tokens
# of English so this is comfortable.
_BRIEF_MAX_TOKENS = 900


class BriefGenerator:
    """Stateless wrapper around the LLM call. The factory builds one
    notifier reference; ``generate`` reads it for the post-write push."""

    def __init__(self, notifier: Optional[Notifier]) -> None:
        self._notifier = notifier

    @property
    def notifier(self) -> Optional[Notifier]:
        """Public accessor for the wired notification backend. ``None``
        if no backend is configured (Pushover/Apprise unset)."""
        return self._notifier

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def generate(self, *, session: AsyncSession, tone: str = "terse") -> Optional[Brief]:
        """Build a new Brief for the requested tone. Returns the row, or
        None if generation was skipped (no provider, empty digest, etc.)."""
        provider = router.provider_for("brief")
        if provider is None:
            logger.info("brief: no LLM provider configured — skipping generation")
            return None
        if tone not in ("terse", "narrative", "alert"):
            tone = "terse"

        entries = await self._select_entries(session, limit=_DIGEST_LIMIT)
        if not entries:
            logger.info("brief: no recent entries — skipping generation")
            return None

        # Resolve the window once and thread it into the prompt so the
        # LLM sees the actual value the selector used (rather than a
        # hardcoded "24h" that lies when the operator changed the knob).
        # ``_select_entries`` re-reads the same value internally — that's
        # a cache hit (5s TTL) so it's a no-op cost.
        window_hours = await self._resolve_window_hours()
        prompt = self._build_prompt(entries, tone, window_hours)
        try:
            content = await provider.complete(prompt, max_tokens=_BRIEF_MAX_TOKENS)
        except ProviderError as exc:
            logger.warning("brief: LLM call failed: %s", exc)
            return None

        content = (content or "").strip()
        if not content:
            logger.warning("brief: LLM returned empty content — skipping persist")
            return None

        # ``delivered_at`` is set when we successfully push the notification
        # in ``_dispatch``. Persist first, then notify — the row exists
        # even if the push fails (the user can still see it on the dashboard).
        brief = Brief(tone=tone, content=content, meta={})
        session.add(brief)
        await session.flush()
        await session.refresh(brief)

        await self._dispatch(brief)
        return brief

    @staticmethod
    def skip_reason() -> str:
        """Why ``generate`` would return None right now. Cheap to call —
        used by the route to surface a precise 503 detail."""
        if router.provider_for("brief") is None:
            return "no LLM provider configured (set ANTHROPIC_API_KEY / OPENAI_API_KEY / GROQ_API_KEY, or run Ollama)"
        # If we got here on a previous attempt and returned None for
        # "no recent entries", we can't know without a DB query — so
        # the default reason is the LLM-failure path, which is by far
        # the most common cause in practice.
        return "LLM call failed or returned empty content (check backend logs)"

    async def generate_alert(
        self, *, session: AsyncSession, slug: str, source_count: int
    ) -> Optional[Brief]:
        """Short-form alert for a single convergence cluster.

        We don't regenerate the whole digest for an alert — the digest
        for the day already exists. Instead, we ask the LLM for a
        one-sentence ``alert`` tone summary of the cluster and persist
        that as its own Brief row so the dashboard surfaces it.
        """
        provider = router.provider_for("brief")
        if provider is None:
            return None

        entries = await self._select_entries_by_slug(session, slug, limit=6)
        if len(entries) < 2:
            return None

        window_hours = await self._resolve_window_hours()
        prompt = (
            f"These {len(entries)} stories all appear to cover the same event "
            f"(seen across {source_count} sources, ingested in the last {window_hours}h):\n\n"
            + self._format_entries(entries)
            + "\n\nWrite ONE sentence (max 30 words) capturing the event and why it matters. "
            "Lead with the fact, not 'this story'. No bullet points, no preamble."
        )
        try:
            content = await provider.complete(prompt, max_tokens=120)
        except ProviderError as exc:
            logger.warning("brief: alert LLM call failed: %s", exc)
            return None
        content = (content or "").strip()
        if not content:
            return None

        brief = Brief(
            tone="alert",
            content=content,
            meta={"alert_slugs": [slug], "source_count": source_count},
        )
        session.add(brief)
        await session.flush()
        await session.refresh(brief)
        await self._dispatch(brief)
        return brief

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    # Bounds on the lookback window. Negative or absurdly large values
    # are easy to typo (or a malicious operator can set BRIEF_WINDOW_HOURS=0
    # and silently disable the brief). Clamp to a sane range — 1h min
    # keeps the brief responsive to "what just landed", 168h (1 week) max
    # keeps the prompt from filling with stale content.
    _WINDOW_MIN_HOURS = 1
    _WINDOW_MAX_HOURS = 168
    _WINDOW_DEFAULT_HOURS = 24

    @classmethod
    async def _resolve_window_hours(cls) -> int:
        """Read the lookback window from runtime settings / env.

        ``runtime_settings.get`` returns a string from the DB or env
        (lines 142 / 147-151 in runtime_settings.py). We cast to int and
        clamp to [_WINDOW_MIN_HOURS, _WINDOW_MAX_HOURS]. Bad values
        (non-numeric, out of range) fall back to the default — a brief
        that's slightly off is better than one that crashes the
        scheduler."""
        raw = await runtime_settings.get(
            "brief.window_hours", default=cls._WINDOW_DEFAULT_HOURS
        )
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return cls._WINDOW_DEFAULT_HOURS
        if n < cls._WINDOW_MIN_HOURS or n > cls._WINDOW_MAX_HOURS:
            return cls._WINDOW_DEFAULT_HOURS
        return n

    @staticmethod
    async def _select_entries(session: AsyncSession, *, limit: int) -> list[tuple[Entry, Source]]:
        """Top-N entries ingested in the lookback window, by composite_score.

        Filter is on ``fetched_at`` (when the row landed in our DB), not
        ``published_at`` (when the source article was published). Wikipedia
        "on this day" entries have very old ``published_at`` values but
        are ingested today — using ``fetched_at`` keeps the brief focused
        on actual recent content. The dashboard's browse view still
        sorts by ``published_at`` so historical entries stay visible.
        Joined to Source so the prompt can show category + source name.
        """
        window_hours = await BriefGenerator._resolve_window_hours()
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
        stmt = (
            select(Entry, Source)
            .join(Source, Entry.source_id == Source.id)
            .where(Entry.fetched_at >= since)
            .order_by(desc(Entry.composite_score))
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        return [(e, s) for e, s in rows]

    @staticmethod
    async def _select_entries_by_slug(
        session: AsyncSession, slug: str, *, limit: int
    ) -> list[tuple[Entry, Source]]:
        """Recent entries whose normalized title matches ``slug``. Used by
        the alert path to feed the LLM just the cluster, not the full
        feed. Same ``fetched_at`` filter as ``_select_entries``."""
        from app.scoring import composite as composite_scorer

        window_hours = await BriefGenerator._resolve_window_hours()
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
        stmt = (
            select(Entry, Source)
            .join(Source, Entry.source_id == Source.id)
            .where(Entry.fetched_at >= since)
            .order_by(desc(Entry.composite_score))
            .limit(limit * 5)  # over-fetch; filter post-hoc by slug
        )
        rows = (await session.execute(stmt)).all()
        return [
            (e, s)
            for e, s in rows
            if composite_scorer.title_slug(e.title) == slug
        ][:limit]

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _format_entries(entries: list[tuple[Entry, Source]]) -> str:
        """Render a compact entry list for the prompt. One entry per line:
        ``[source · category · score] title — summary``."""
        lines: list[str] = []
        for entry, source in entries:
            score = entry.composite_score or 0.0
            title = (entry.title or "").strip()
            summary = ""
            if entry.meta and isinstance(entry.meta, dict):
                summary = (
                    entry.meta.get("summary")
                    or entry.meta.get("vulnerabilityName")
                    or entry.meta.get("shortDescription")
                    or ""
                )
                # Truncate aggressively — the digest doesn't need the
                # full CVE description.
                summary = (str(summary).strip()[:240]).strip()
            line = f"[{source.name} · {source.category} · {score:.0f}] {title}"
            if summary:
                line += f" — {summary}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _build_prompt(entries: list[tuple[Entry, Source]], tone: str, window_hours: int) -> str:
        tone_blurb = {
            "terse": (
                "Write a brief, dense digest. Skip the preamble. Lead with the "
                "most important fact."
            ),
            "narrative": (
                "Write a short narrative digest (3-5 sentences) that flows like "
                "a newsletter intro. Conversational but factual."
            ),
            "alert": (
                "Write ONE short paragraph (2-3 sentences) summarizing the most "
                "important development."
            ),
        }.get(tone, "")

        return (
            f"You are the editor of a personal intelligence brief. "
            f"Tone: {tone}. {tone_blurb}\n\n"
            f"Format your output exactly like this — no extra prose, no markdown:\n\n"
            f"  TODAY IN ONE SENTENCE\n"
            f"  <one sentence capturing the single most important development>\n\n"
            f"  HIGHLIGHTS\n"
            f"  - <headline> — <one sentence why it matters>\n"
            f"  - ... (3-5 items)\n\n"
            f"  WATCH\n"
            f"  - <item worth keeping an eye on, lower priority>\n"
            f"  - ... (1-3 items)\n\n"
            f"Top {len(entries)} entries ingested in the last {window_hours}h:\n\n"
            f"{BriefGenerator._format_entries(entries)}\n"
        )

    # ------------------------------------------------------------------
    # Notification dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, brief: Brief) -> None:
        """Push the Brief to the notification backend (best-effort).

        Marks ``delivered_at`` on success. Failure is logged but doesn't
        raise — a broken notifier must not break Brief generation."""
        if self._notifier is None:
            return
        try:
            date_label = brief.generated_at.strftime("%Y-%m-%d")
            title = f"Popping brief · {date_label}"
            body = brief.content
            await self._notifier.send(title=title, body=body)
            brief.delivered_at = dt.datetime.now(dt.timezone.utc)
            logger.info("brief: dispatched id=%d tone=%s", brief.id, brief.tone)
        except Exception:
            logger.exception("brief: dispatch failed id=%d", brief.id)
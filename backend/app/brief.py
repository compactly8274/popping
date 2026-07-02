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
import re
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm import ProviderError, router
from app.models import Brief, Entry, Source
from app.notify import Notifier
from app import brief_filter
from app import runtime_settings

logger = logging.getLogger("popping.brief")


# How many top entries the digest covers. Large enough to give the LLM
# real choice; small enough that the prompt stays under a few KB.
_DIGEST_LIMIT = 25
# Brief output budget. 600 tokens is enough for the headline sentence
# (~40) + 5 highlights (~50 each = 250) + 3 watch items (~30 each = 90)
# = ~380, with a ~220-token buffer for verbose models or long
# summaries. The previous 400-budget was tight enough that some
# models (notably ``glm-5.2:cloud``) truncated mid-sentence after
# 1-2 highlight bullets. 600 gives the model room to complete the
# WATCH section without sacrificing the stop-sequence defense
# against CoT leaks. Pushover's body limit is ~4 KB (~1k tokens of
# English), so 600 is comfortably within the notification budget too.
_BRIEF_MAX_TOKENS = 600

# Stop sequences for the brief. Each entry is a string the model is
# told to halt on — the moment any of these appears, generation stops
# and the partial response is returned. The brief format is fixed:
# TODAY IN ONE SENTENCE → headline → HIGHLIGHTS → - bullets → WATCH
# → - bullets. Anything else is noise we want to cut at the source.
#
# The list targets the specific leak patterns we've observed:
#   - ``\n\n1. `` / ``\n\n2. `` — numbered analysis headings ("1. Role",
#     "2. Evaluate Source Entries"). The model was treating the
#     prompt's constraints as a task outline and numbering its work.
#   - ``**`` — markdown bold. The model was bolding section labels.
#   - "Source Material:" — the exact phrase the model leaked as a
#     header above the entries dump.
#   - "# Brief" — markdown H1 header the model sometimes prepends.
#
# Anthropic caps ``stop_sequences`` at 4. We list 4 here so every
# provider gets the full set. If you're on Ollama and want more
# (e.g. "Note to the editor", "Constraint"), add them at the end
# and update the Anthropic provider to slice ``[:4]`` if needed —
# but the 4 below catch every leak we've actually seen in the wild.
_BRIEF_STOP_SEQUENCES: list[str] = [
    "\n\n1. ",
    "\n**",
    "Source Material",
    "# Brief",
]

# Source slug that's categorically historical and never belongs in a
# "this present day" brief. The source itself stays enabled — its
# entries still surface in the dashboard's browse view (sorted by
# ``published_at``) — but we filter it out of the brief's candidate
# set so the LLM doesn't pick a 1950 Korea story as today's lead.
#
# Excluding by source slug (not by category) because the category is
# ``news``, shared with real news sources. A new custom source named
# ``my_history_feed`` would still flow into the brief — the slug
# match is a deliberate, narrow opt-out, not a category filter.
_HISTORICAL_SOURCE_SLUGS: frozenset[str] = frozenset({"wikipedia_on_this_day"})


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
            content = await provider.complete(
                prompt,
                max_tokens=_BRIEF_MAX_TOKENS,
                stop=_BRIEF_STOP_SEQUENCES,
            )
        except ProviderError as exc:
            logger.warning("brief: LLM call failed: %s", exc)
            return None

        content = (content or "").strip()
        if not content:
            logger.warning("brief: LLM returned empty content \u2014 skipping persist")
            return None

        # Truncation retry. Some models (notably
        # ``glm-5.2:cloud`` via Ollama Cloud) decide to
        # stop generating after 1-2 highlight bullets,
        # even with 600-token headroom. The prompt
        # strengthening above addresses the common case;
        # this retry catches the residual case where the
        # model still truncates. We parse the response
        # in the same shape the frontend uses (TODAY IN
        # ONE SENTENCE / HIGHLIGHTS / WATCH sections)
        # and retry if the WATCH section is missing. The
        # retry uses a continuation prompt that asks
        # the model to finish the brief from where it
        # stopped, with a fresh max_tokens budget (300)
        # that is enough to complete a 1-bullet WATCH
        # section but not enough to leak CoT.
        truncated_reason = self._is_truncated(content)
        if truncated_reason and getattr(self, "_allow_retry", True):
            logger.info(
                "brief: response truncated (%s) \u2014 retrying with continuation prompt",
                truncated_reason,
            )
            continuation = (
                "Continue the brief below from where you stopped. Do "
                "not restart, do not repeat, do not echo the existing "
                "lines. Add only the missing section.\n\n"
                f"{content}\n"
            )
            try:
                retry = await provider.complete(
                    continuation,
                    max_tokens=300,
                    stop=_BRIEF_STOP_SEQUENCES,
                )
            except ProviderError as exc:
                logger.warning("brief: retry LLM call failed: %s", exc)
            else:
                retry = (retry or "").strip()
                if retry:
                    sep = "" if content.endswith("\n") else "\n"
                    content = (content + sep + retry).strip()
                    if self._is_truncated(content):
                        logger.warning(
                            "brief: retry still truncated \u2014 shipping partial brief (%d chars)",
                            len(content),
                        )
                    else:
                        logger.info(
                            "brief: retry completed the brief (%d chars)",
                            len(content),
                        )

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
            "Lead with the fact, not 'this story'. No bullet points, no preamble, "
            "no markdown, no bold, no headers, no analysis. Output only the sentence."
        )
        try:
            # Same stop sequences as the digest. The alert prompt is
            # already tight ("write ONE sentence… output only the
            # sentence"), but the same model tendencies apply — the
            # model might still leak if asked to analyze a cluster.
            content = await provider.complete(
                prompt,
                max_tokens=120,
                stop=_BRIEF_STOP_SEQUENCES,
            )
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
        """Top-N entries ingested in the lookback window, by composite_score
        with a convergence multiplier applied.

        Sort key is ``composite_score * convergence_multiplier(slug)``
        — a story appearing in 3+ sources gets a 1.20× boost, 2 sources
        gets 1.10×, 1 source gets 1.0. Same multipliers the For You
        route applies, so a brief pick is consistent with what the
        user sees when they scroll their personal feed.

        Clickbait titles (ALL-CAPS, listicles, "shocking"-style
        adjectives, emoji noise) are dropped at this layer. The
        dashboard feed still surfaces them — only the brief editor
        filters them out, since the brief is a curated editorial
        product and the user already opted in to it by generating.

        Filter is on ``fetched_at`` (when the row landed in our DB), not
        ``published_at`` (when the source article was published). Wikipedia
        "on this day" entries have very old ``published_at`` values but
        are ingested today — using ``fetched_at`` keeps the brief focused
        on actual recent content. The dashboard's browse view still
        sorts by ``published_at`` so historical entries stay visible.

        Historical-content sources (currently just
        ``wikipedia_on_this_day``) are excluded by slug. Their entries
        still surface in the dashboard's browse view; only the brief
        skips them. Joined to Source so the prompt can show category +
        source name.

        Convergence counts are computed in the same window as the
        ``for you`` route (``settings.convergence_window_hours``,
        default 24) so a story trending across feeds right now is
        the one that bubbles up here.
        """
        # Over-fetch so convergence-boosted entries still have room to
        # climb into the result set. Cap at 500 — same bound as the
        # foryou route — so the post-filter pass stays cheap.
        over_fetch = min(max(limit * 4, 200), 500)
        window_hours = await BriefGenerator._resolve_window_hours()
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
        stmt = (
            select(Entry, Source)
            .join(Source, Entry.source_id == Source.id)
            .where(Entry.fetched_at >= since)
            .where(Source.name.notin_(_HISTORICAL_SOURCE_SLUGS))
            .order_by(desc(Entry.composite_score))
            .limit(over_fetch)
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            return []

        # Clickbait filter — drop loud, deterministic patterns. The
        # dashboard keeps these; the brief doesn't.
        filtered = [
            (e, s) for e, s in rows
            if not brief_filter.is_clickbait(e.title)
        ]
        if not filtered:
            return []

        # Convergence boost — same window as /api/foryou so brief
        # picks are consistent with the personal feed's notion of
        # "trending right now". The shared convergence helper caches
        # the result for 30s, so a brief generation happening in the
        # same window as a /api/foryou poll is free.
        from app.scoring import composite as composite_scorer
        from app.scoring import convergence as conv_helper

        conv_counts = await conv_helper.counts(
            session, settings.convergence_window_hours,
        )

        scored: list[tuple[float, Entry, Source]] = []
        for entry, source in filtered:
            slug = composite_scorer.title_slug(entry.title)
            mult = composite_scorer.convergence_multiplier(
                conv_counts.get(slug, 1)
            )
            base = entry.composite_score or 0.0
            scored.append((base * mult, entry, source))

        # Sort descending by boosted score; tie-break on published_at
        # so two equal-scored stories prefer the more recent one.
        scored.sort(
            key=lambda t: (
                t[0],
                t[1].published_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            ),
            reverse=True,
        )
        return [(e, s) for _score, e, s in scored[:limit]]

    @staticmethod
    async def _select_entries_by_slug(
        session: AsyncSession, slug: str, *, limit: int
    ) -> list[tuple[Entry, Source]]:
        """Recent entries whose normalized title matches ``slug``. Used by
        the alert path to feed the LLM just the cluster, not the full
        feed. Same ``fetched_at`` filter as ``_select_entries`` plus the
        historical-source exclusion."""
        from app.scoring import composite as composite_scorer

        window_hours = await BriefGenerator._resolve_window_hours()
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
        stmt = (
            select(Entry, Source)
            .join(Source, Entry.source_id == Source.id)
            .where(Entry.fetched_at >= since)
            .where(Source.name.notin_(_HISTORICAL_SOURCE_SLUGS))
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
        ``[source · category · score] title — summary``.

        ``summary`` here is the best long-form description we can find
        in ``Entry.meta`` — see ``brief_filter.extract_summary`` for the
        priority order across sources. Up to 600 chars (up from the
        previous 240) so the LLM has real context to summarize from
        rather than just a fragment of the lede.
        """
        lines: list[str] = []
        for entry, source in entries:
            score = entry.composite_score or 0.0
            title = (entry.title or "").strip()
            summary = brief_filter.extract_summary(entry.meta or {}, max_chars=600)
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

        # Prompt design notes — this is the third pass on the brief
        # prompt. Each pass was driven by a specific leak the user
        # pasted back. The current shape was tuned against:
        #
        #   1. The verbose-model "1. **Role:** / 2. **Select:**" leak
        #      (c510f57). Fixed by removing numbered constraint
        #      headers and the BAD-vs-GOOD example block.
        #   2. The "**Analyze the Request:**" leak (2672d15). Fixed
        #      by dropping markdown emphasis from the prompt and
        #      adding stop sequences on ``\n**``.
        #   3. The CURRENT leak: the minimal GOOD example block
        #      (``- topic A — why it matters in one sentence``, the
        #      ``(3 to 5 bulleted lines)`` parenthetical from the
        #      format_directive, and the words "Watch" + "Let's pick
        #      the most important facts:") was being echoed back
        #      verbatim. The model literally reproduced the format
        #      example as the highlights/watch sections, then dumped
        #      its reasoning ("Wait, the instructions say...") after.
        #
        # Fix for the current leak: the prompt no longer contains
        # any example text the model could echo. The format_directive
        # is the only place the format is described, and an explicit
        # "no echo" clause sits between the directive and the entries
        # to tell the model its output should be the brief itself,
        # not a description of the brief. The frontend parser also
        # gains defense-in-depth (see
        # ``frontend/src/components/BriefCard.tsx``).

        # Editing guidance. Tells the model to read the description
        # AFTER the em-dash and summarize from there, not to parrot
        # the title. Sensational titles can slip past the regex
        # filter ("Why everyone's wrong about X"), and without this
        # hint the model often amplifies them rather than cutting to
        # what's actually happening.
        editing_guidance = (
            "For each highlight, summarize what is ACTUALLY happening "
            "based on the description after the em-dash, not the title "
            "alone. If the title is editorialized or sensational, write "
            "the plain factual version. Lead with the fact."
        )

        # Format directive. Describes the shape in prose; no example
        # text the model could copy. The bullet count is given as a
        # range ("between three and five") rather than the literal
        # string "3 to 5" — the previous parenthetical was being
        # echoed as ``(3 to 5 bulleted lines)`` in the output.
        format_directive = (
            "Reply with the brief only. Output shape, in this exact "
            "order: first, a line that is literally TODAY IN ONE SENTENCE "
            "on its own. Next, one sentence summarising the single most "
            "important development. Then a blank line. Then a line that "
            "is literally HIGHLIGHTS on its own. Then between three and "
            "five bulleted lines, each starting with a hyphen, each "
            "ending with a period. Then a blank line. Then a line that "
            "is literally WATCH on its own. Then between one and three "
            "bulleted lines, each starting with a hyphen, each ending "
            "with a period. Nothing else in the reply \u2014 no preamble, no "
            "analysis, no reasoning, no explanations, no labels, no "
            "headers, no bold, no italic, no markdown of any kind. "
            "The brief is not complete until you have written the WATCH "
            "section with at least one bullet. Do not stop after the "
            "HIGHLIGHTS bullets \u2014 you must continue to the WATCH section."
        )

        # Anti-echo clause. Some models (notably the thinking-style
        # ones on Ollama Cloud) treat the prompt as a checklist and
        # write back a paraphrase of the instructions before the
        # actual content. The leaked output we fixed had the model
        # reproducing the format example AND a stream-of-thought
        # preamble ("Let's pick the most important facts:"). This
        # clause tells the model its output is the brief itself, not
        # a description of how to write the brief. Sits between the
        # directive and the entries so the model reads it as a
        # constraint on the OUTPUT, not a directive to narrate.
        no_echo_clause = (
            "Do not paraphrase, echo, restate, or summarise the "
            "instructions above in your reply. The reply is the brief "
            "itself, not a description of the brief. Do not include any "
            "reasoning, plan, checklist, or meta-commentary before or "
            "after the brief."
        )

        scope_clause = (
            f"All entries below are real news ingested in the last {window_hours} "
            "hours. Some titles carry a year prefix because the source is a "
            "curated history feed; ignore those entries entirely — they are "
            "not candidates for the brief."
        )

        # No example block, no numbered headers, no role label, no
        # markdown, no "Source Material:" label. The directive
        # describes the format in prose; the model has to write the
        # content from the entries.
        return (
            f"You are the editor of a personal intelligence brief. Tone: {tone}. "
            f"{tone_blurb}\n\n"
            f"{editing_guidance}\n\n"
            f"{format_directive}\n\n"
            f"{no_echo_clause}\n\n"
            f"{scope_clause}\n\n"
            f"Candidate entries:\n\n"
            f"{BriefGenerator._format_entries(entries)}\n"
        )

    @staticmethod
    def _is_truncated(content: str) -> Optional[str]:
        """Return a short reason if the brief looks truncated,
        ``None`` if it appears complete.

        Heuristic: the brief is "complete" if it has all
        three section headers (TODAY IN ONE SENTENCE,
        HIGHLIGHTS, WATCH) and the WATCH section has at
        least one bullet. We don't try to count highlights
        here \u2014 the prompt asks for 3-5 and the parser
        is robust to whatever count it gets. A brief
        with HIGHLIGHTS but no WATCH is the most common
        truncation pattern we see in practice.

        Returns a human-readable reason string so the
        caller can log it. ``None`` means the brief
        looks complete.
        """
        if not content:
            return "empty"
        if "TODAY IN ONE SENTENCE" not in content:
            return "missing headline header"
        if "HIGHLIGHTS" not in content:
            return "missing highlights header"
        if "WATCH" not in content:
            return "missing watch section"
        watch_idx = content.upper().rfind("WATCH")
        after_watch = content[watch_idx:]
        if not re.search(r"^\s*-\s", after_watch, re.MULTILINE):
            return "watch section has no bullets"
        return None

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
"""LLM-based feed discovery — grows the recommendation pool beyond the
editorial seed (see ``app.feed_recommendations``).

Two trigger paths, both landing here:
    - Automatic: ``POST /api/sources`` schedules a background call
      scoped to the category (and name/blurb context) of the feed the
      user just added — "find me more like this."
    - Manual: ``POST /api/feed-recommendations/discover`` calls this
      synchronously for a chosen (or inferred) category — "find more
      feeds" button in the Recommended tab.

Suggestions come from the same LLM provider chain the Brief generator
and podcast-transcript summarizer use (``router.providers_for("brief")``
— no new provider wiring). Every suggested URL is fetch-validated with
the same SSRF guard + RSS parser the "Test" button uses before it's
persisted, so a hallucinated or dead URL never reaches the pool.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ProviderError, router
from app.models import FeedRecommendationCandidate, Source
from app.sources.rss import fetch_rss
from app.url_safety import check_url_safe

logger = logging.getLogger("popping.feed_discovery")

# Same shape POST /api/sources validates names against (routes/sources.py's
# _NAME_RE) — duplicated rather than imported to avoid a routes -> this
# module -> routes import cycle (the route module calls into this one for
# the auto-trigger and the /discover endpoint).
_NAME_RE = re.compile(r"^[a-z0-9_]{1,120}$")
_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")

_MAX_SUGGESTIONS_REQUESTED = 5
_BLURB_MAX_LEN = 200
_LLM_MAX_TOKENS = 800

# Window ``recent_llm_candidate_count`` looks back over. The route
# layer (``routes/sources.py``) owns the actual cap/policy decision
# for the auto-trigger cooldown; this module just answers "how many
# recently" so that policy has something to compare against.
_RECENT_WINDOW = timedelta(days=7)


def _slugify(raw: str) -> str:
    """Best-effort conversion of an LLM-suggested display name into
    the ``^[a-z0-9_]{1,120}$`` shape ``POST /api/sources`` requires.
    Returns "" if nothing usable survives (caller skips the row)."""
    s = _SANITIZE_RE.sub("_", raw.strip().lower()).strip("_")
    return s[:120]


def _strip_code_fence(text: str) -> str:
    """LLMs frequently wrap JSON in a ```json ... ``` fence despite
    being told not to. Strip it if present; otherwise return as-is."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"```\s*$", "", t)
    return t.strip()


def _build_prompt(category: str, context: str, exclude_names: set[str], limit: int) -> str:
    exclude_list = ", ".join(sorted(exclude_names)) or "(none)"
    return (
        f"Suggest up to {limit} real, currently active RSS or Atom feed "
        f"URLs in the \"{category}\" category. {context}\n\n"
        "Only suggest feeds you are confident actually exist and are "
        "still publishing — do not invent plausible-sounding URLs. "
        f"Do not suggest any of these (already covered): {exclude_list}.\n\n"
        "Respond with ONLY a JSON array, no other text, no markdown "
        "fence, shaped exactly like:\n"
        '[{"name": "short_lowercase_slug", "url": "https://...", '
        '"blurb": "one line describing the feed"}]'
    )


async def _ask_llm_for_suggestions(
    category: str, context: str, exclude_names: set[str], limit: int
) -> tuple[list[dict], str | None]:
    """Returns ``(suggestions, note)``. ``note`` is None when a
    provider actually produced usable output (even an empty list —
    "the LLM ran and had nothing new to add" needs no further
    explanation). Otherwise it's a short, specific reason why not —
    "no LLM provider configured" (nothing to even try; note the env
    chain always includes local Ollama as a final fallback, so this
    only fires when a *pinned* provider has no usable auth) or the
    last provider's actual error ("groq: 401 unauthorized", "ollama:
    connection refused", ...). Surfacing the real reason instead of a
    generic "nothing found" is what tells "you need to configure an
    API key" apart from "this category is temporarily saturated" —
    both look identical as a bare ``added=0`` otherwise.
    """
    providers = router.providers_for("brief")
    if not providers:
        logger.info("feed_discovery: no LLM provider configured — skipping")
        return [], "no LLM provider configured"
    prompt = _build_prompt(category, context, exclude_names, limit)
    last_note: str | None = None
    for candidate in providers:
        try:
            content = await candidate.complete(prompt, max_tokens=_LLM_MAX_TOKENS)
        except ProviderError as exc:
            logger.warning("feed_discovery: LLM call failed on %s: %s — trying next provider", candidate.name, exc)
            last_note = f"{candidate.name}: {exc}"
            continue
        content = _strip_code_fence(content or "")
        if not content:
            last_note = f"{candidate.name}: returned an empty response"
            continue
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            logger.warning("feed_discovery: %s returned non-JSON output, skipping", candidate.name)
            last_note = f"{candidate.name}: returned non-JSON output"
            continue
        if isinstance(parsed, dict):
            # Some providers wrap the array in {"feeds": [...]}. despite the prompt.
            parsed = parsed.get("feeds") or parsed.get("suggestions") or []
        if not isinstance(parsed, list):
            last_note = f"{candidate.name}: returned an unexpected response shape"
            continue
        return [item for item in parsed if isinstance(item, dict)], None
    logger.warning("feed_discovery: all configured LLM providers failed or returned unusable output")
    return [], last_note or "all configured providers failed"


async def _validate_feed_url(url: str) -> bool:
    """Same guard the "Test" button applies: SSRF-safe, then a real
    fetch that returns at least one item. Any failure (network,
    parse, unsafe URL) means "don't trust this suggestion"."""
    safe, reason = check_url_safe(url)
    if not safe:
        logger.info("feed_discovery: rejected %s (%s)", url, reason)
        return False
    try:
        items = await fetch_rss(url)
    except Exception as exc:  # noqa: BLE001 - any fetch/parse failure disqualifies the suggestion
        logger.info("feed_discovery: validation fetch failed for %s: %s", url, exc)
        return False
    return len(items) > 0


async def _existing_names_and_urls(session: AsyncSession) -> tuple[set[str], set[str]]:
    source_rows = (await session.execute(select(Source.name, Source.url))).all()
    candidate_rows = (
        await session.execute(select(FeedRecommendationCandidate.name, FeedRecommendationCandidate.url))
    ).all()
    names = {r.name for r in source_rows} | {r.name for r in candidate_rows}
    urls = {r.url for r in source_rows} | {r.url for r in candidate_rows}
    return names, urls


async def recent_llm_candidate_count(session: AsyncSession, category: str) -> int:
    """Number of ``source="llm"`` rows discovered for ``category`` in
    the last ``_RECENT_WINDOW``. Used to cooldown the auto-trigger."""
    cutoff = datetime.now(timezone.utc) - _RECENT_WINDOW
    stmt = select(func.count()).select_from(FeedRecommendationCandidate).where(
        FeedRecommendationCandidate.category == category,
        FeedRecommendationCandidate.source == "llm",
        FeedRecommendationCandidate.created_at >= cutoff,
    )
    return int((await session.execute(stmt)).scalar_one())


async def discover_candidates(
    session: AsyncSession,
    *,
    category: str,
    context: str,
    discovered_from_source_id: int | None = None,
    limit: int = _MAX_SUGGESTIONS_REQUESTED,
) -> tuple[list[FeedRecommendationCandidate], str | None]:
    """Ask the LLM for feed suggestions in ``category``, validate each
    with a real fetch, and persist the ones that check out as
    ``source="llm"`` candidates.

    Returns ``(created, note)``. ``created`` is the rows actually
    created (validation failures and name/url collisions are
    silently dropped — this is best-effort enrichment, not a
    user-facing error path). ``note`` is None when everything worked
    as well as it reasonably could (created some rows, or the LLM
    itself reported nothing new); otherwise a short, specific reason
    ``created`` is empty — see ``_ask_llm_for_suggestions`` for the
    provider-failure notes, or below for "provider worked but nothing
    survived validation".
    """
    existing_names, existing_urls = await _existing_names_and_urls(session)
    suggestions, note = await _ask_llm_for_suggestions(category, context, existing_names, limit)

    created: list[FeedRecommendationCandidate] = []
    seen_names = set(existing_names)
    seen_urls = set(existing_urls)
    for raw in suggestions[:limit]:
        name = _slugify(str(raw.get("name") or ""))
        url = str(raw.get("url") or "").strip()
        blurb = str(raw.get("blurb") or "").strip()[:_BLURB_MAX_LEN]
        if not name or not _NAME_RE.match(name) or not url or not blurb:
            continue
        if name in seen_names or url in seen_urls:
            continue
        if not url.startswith(("http://", "https://")):
            continue
        if not await _validate_feed_url(url):
            continue
        seen_names.add(name)
        seen_urls.add(url)
        row = FeedRecommendationCandidate(
            name=name,
            category=category,
            url=url,
            blurb=blurb,
            source="llm",
            discovered_from_source_id=discovered_from_source_id,
        )
        session.add(row)
        created.append(row)

    if created:
        await session.commit()
        for row in created:
            await session.refresh(row)
        logger.info("feed_discovery: added %d llm candidate(s) for category=%s", len(created), category)
        return created, None
    if note is not None:
        # Provider-level failure — already a specific note.
        return created, note
    if suggestions:
        # The provider worked and suggested something, but every
        # suggestion was malformed, a duplicate, or failed the
        # fetch-validation probe — distinct from "the LLM said there
        # was nothing new" (which is ``suggestions == []`` with
        # ``note is None``, and needs no explanation at all).
        return created, f"got {len(suggestions)} suggestion(s) but none passed validation"
    return created, None

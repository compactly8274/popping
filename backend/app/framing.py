"""Framing Watch: detect the same underlying story republished under
different headlines by different outlets, so media-framing differences
are visible side-by-side.

Distinct from ``app.scoring.convergence`` (which groups by normalized
TITLE — the "several outlets ran the identical headline" case, used
for the For You convergence boost). This module groups by EMBEDDING
similarity instead, which is what catches the interesting case: same
story, different headline/framing per outlet.

Pipeline (``cluster_recent_entries``, run hourly by the scheduler):
    1. Pull every entry published in the last ``framing_window_hours``
       that has an embedding (``Entry.embedding`` — reused as-is, no
       new embedding work; see the module-level caveat below).
    2. Union-find over pairs whose cosine similarity clears
       ``framing_similarity_threshold`` AND whose published_at are
       within the window of each other.
    3. Reconcile each 2+-member group against existing
       ``Entry.story_cluster_id`` values: adopt an existing cluster
       row if any member already carries one, otherwise create a new
       ``StoryCluster`` row. Members that fell out of every current
       group (window aged past them) get their story_cluster_id
       cleared.
    4. Delete any cluster row that's dropped below 2 members (its
       members' FK auto-nulls via ON DELETE SET NULL).
    5. For any cluster with untagged (``framing_tone IS NULL``)
       members, fire ONE batched LLM call classifying every untagged
       headline in that cluster at once — never one call per article.

Caveat on signal strength: ``Entry.embedding`` is built from
``title + " — " + <feed's own short summary blurb>`` (see
``app.scheduler._embed_text``) — there's no full article body text
stored anywhere in this schema (``Entry.body_text`` is currently
always NULL; nothing populates it). So this clusters on title+blurb
similarity, not full-body similarity. Wire-service ledes are often
similar even in just the opening line, so this is a reasonable
first cut, but it's a weaker signal than true full-text comparison —
tune ``framing_similarity_threshold`` accordingly if you see too many
false positives/negatives.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections import defaultdict
from typing import Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm import ProviderError, router
from app.models import Entry, StoryCluster
from app.scoring.personal import _cosine

logger = logging.getLogger("popping.framing")


# --- wire-source attribution -------------------------------------------------

# Byline/dateline patterns for the wire services explicitly named in the
# feature request. Best-effort only — a cluster forms on embedding
# similarity regardless of whether any pattern matches; this just adds
# an optional label. Checked against title + summary blurb (the only
# text available — see module docstring).
_WIRE_SOURCE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AP", re.compile(r"\bassociated press\b|\(\s*ap\s*\)|—\s*ap\s*[—-]", re.IGNORECASE)),
    ("Reuters", re.compile(r"\breuters\b", re.IGNORECASE)),
    ("AFP", re.compile(r"\bagence france-presse\b|\bafp\b", re.IGNORECASE)),
]


def detect_wire_source(title: str, summary: Optional[str]) -> Optional[str]:
    """Best-effort wire-service detection from title + summary text.
    Returns None (not "unknown" or "") when nothing matches — a
    cluster stands on its own without this label; see module docstring."""
    haystack = f"{title or ''} {summary or ''}"
    for name, pattern in _WIRE_SOURCE_PATTERNS:
        if pattern.search(haystack):
            return name
    return None


# --- clustering ---------------------------------------------------------------


class _UnionFind:
    """Minimal union-find over entry ids. Path compression only (no
    union-by-rank) — the entry counts this runs over (a personal
    dashboard's 48h window) are far too small for that to matter."""

    def __init__(self, ids: list[int]) -> None:
        self._parent = {i: i for i in ids}

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


async def cluster_recent_entries(session: AsyncSession) -> dict:
    """Recompute Framing Watch clusters over the trailing
    ``framing_window_hours``. Full rescan every run (not incremental)
    — simple, and cheap enough at personal-dashboard entry volume.
    Returns a small summary dict for logging/tests.
    """
    threshold = settings.framing_similarity_threshold
    window_hours = settings.framing_window_hours
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)

    # Release any entry that has aged fully out of the window but
    # still carries a story_cluster_id from an earlier run — without
    # this, such an entry is invisible to everything below (the
    # windowed SELECT never fetches it) and its stale FK would keep
    # an otherwise-orphaned cluster looking "full" forever, since
    # _delete_orphan_clusters counts against the real table, not this
    # run's candidate pool.
    await session.execute(
        update(Entry)
        .where(Entry.published_at < since, Entry.story_cluster_id.isnot(None))
        .values(story_cluster_id=None)
    )
    await session.commit()

    rows = (
        await session.execute(
            select(
                Entry.id,
                Entry.title,
                Entry.embedding,
                Entry.published_at,
                Entry.meta,
                Entry.story_cluster_id,
            ).where(Entry.published_at >= since, Entry.embedding.isnot(None))
        )
    ).all()

    if len(rows) < 2:
        await _delete_orphan_clusters(session)
        return {"entries_considered": len(rows), "clusters": 0, "tone_calls": 0}

    # Pairwise union-find. O(n^2) cosine comparisons — fine at the
    # entry counts a personal dashboard's 48h window produces (low
    # hundreds); would need a pgvector ANN index to scale further.
    uf = _UnionFind([r.id for r in rows])
    window_delta = dt.timedelta(hours=window_hours)
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if a.published_at is None or b.published_at is None:
                continue
            if abs(a.published_at - b.published_at) > window_delta:
                continue
            sim = _cosine(a.embedding, b.embedding)
            if sim is not None and sim >= threshold:
                uf.union(a.id, b.id)

    groups: dict[int, list] = defaultdict(list)
    for r in rows:
        groups[uf.find(r.id)].append(r)
    desired_clusters = [g for g in groups.values() if len(g) >= 2]

    touched_member_ids: set[int] = set()
    for group in desired_clusters:
        wire = None
        for r in group:
            summary = (r.meta or {}).get("summary") if isinstance(r.meta, dict) else None
            wire = detect_wire_source(r.title, summary)
            if wire:
                break
        first_seen = min((r.published_at for r in group if r.published_at is not None), default=None)

        existing_ids = {r.story_cluster_id for r in group if r.story_cluster_id is not None}
        if existing_ids:
            # Adopt the lowest existing id — the only way a group can
            # carry more than one existing id is if two previously
            # separate clusters just merged (a new entry bridged them);
            # picking the oldest row keeps the cluster's identity
            # (and any already-computed tone labels) stable rather
            # than arbitrary.
            cluster_id = min(existing_ids)
            await session.execute(
                update(StoryCluster)
                .where(StoryCluster.id == cluster_id)
                .values(wire_source=wire, first_seen_at=first_seen)
            )
        else:
            cluster = StoryCluster(wire_source=wire, first_seen_at=first_seen)
            session.add(cluster)
            await session.flush()
            cluster_id = cluster.id

        member_ids = [r.id for r in group]
        touched_member_ids.update(member_ids)
        await session.execute(
            update(Entry).where(Entry.id.in_(member_ids)).values(story_cluster_id=cluster_id)
        )

    # Entries that were clustered before this run but didn't land in
    # any 2+-member group this time (their window-mate aged out) —
    # release them back to unclustered.
    previously_clustered_ids = {r.id for r in rows if r.story_cluster_id is not None}
    to_release = previously_clustered_ids - touched_member_ids
    if to_release:
        await session.execute(
            update(Entry).where(Entry.id.in_(to_release)).values(story_cluster_id=None)
        )

    await session.commit()
    await _delete_orphan_clusters(session)
    tone_calls = await _tag_untagged_clusters(session)

    return {
        "entries_considered": len(rows),
        "clusters": len(desired_clusters),
        "tone_calls": tone_calls,
    }


async def _delete_orphan_clusters(session: AsyncSession) -> None:
    """Drop any story_clusters row with fewer than 2 members left —
    covers both this run's reconciliation (a group shrank below 2)
    and stale rows from a previous run whose members have since aged
    entirely out of the window. Entry.story_cluster_id auto-nulls via
    ON DELETE SET NULL for anything still pointing at a deleted row."""
    stmt = (
        select(StoryCluster.id)
        .outerjoin(Entry, Entry.story_cluster_id == StoryCluster.id)
        .group_by(StoryCluster.id)
        .having(func.count(Entry.id) < 2)
    )
    orphan_ids = (await session.scalars(stmt)).all()
    if orphan_ids:
        await session.execute(delete(StoryCluster).where(StoryCluster.id.in_(orphan_ids)))
        await session.commit()


# --- headline tone classification --------------------------------------------

_VALID_TONES = ("neutral", "urgent", "alarmist")
_TONE_MAX_TOKENS = 300
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|```\s*$")


def _build_tone_prompt(titles: list[str]) -> str:
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    return (
        "Classify the tone of each news headline below as exactly one "
        "of: neutral, urgent, or alarmist.\n"
        "- neutral: matter-of-fact reporting, no emotional framing.\n"
        "- urgent: conveys real time-pressure or high stakes without "
        "sensationalizing.\n"
        "- alarmist: uses fear, crisis language, or exaggeration beyond "
        "what the situation likely warrants.\n\n"
        f"{numbered}\n\n"
        "Respond with ONLY a JSON array of exactly one tone string per "
        'headline, in the same order, e.g. ["neutral", "alarmist"]. '
        "No other text, no markdown fence."
    )


def _parse_tone_response(content: str, expected_len: int) -> Optional[list[str]]:
    """Extract a tone label list from the model's reply.

    Three input shapes, in order of preference:

    1. Pure JSON: ``["neutral", "alarmist"]`` — works for non-thinking
       models and for thinking models that emit a clean post-CoT
       answer.
    2. JSON inside a markdown fence: stripped via ``_CODE_FENCE_RE``.
    3. JSON embedded in chain-of-thought: thinking models (gpt-oss,
       deepseek-r1, glm-5.2) often return a JSON array at the end
       of a longer reasoning trace. Try ``json.loads`` on the full
       text; if that fails, scan for the first balanced ``[...]``
       and try again. This keeps the parser simple while
       tolerating the model putting the answer in ``thinking``
       (which the provider substitutes into ``response`` via the
       thinking-model fallback).
    """
    text = _CODE_FENCE_RE.sub("", (content or "").strip()).strip()
    if not text:
        return None
    # First try: whole text is JSON.
    data = _try_parse_json_array(text)
    if data is not None:
        return _validate_tone_list(data, expected_len)
    # Second try: extract the first balanced [...] substring.
    bracket_text = _extract_first_json_array(text)
    if bracket_text is not None:
        data = _try_parse_json_array(bracket_text)
        if data is not None:
            return _validate_tone_list(data, expected_len)
    return None


def _try_parse_json_array(text: str) -> Optional[list]:
    """``json.loads`` on ``text``, returning the list only if it parsed
    as a JSON array. Anything else (object, scalar, parse error) is
    None. The framing response is a list of tone strings; an object
    means the model went off-script (e.g. wrapped the array in
    ``{"tones": [...]}``) and we let the bracket-extractor handle it.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _extract_first_json_array(text: str) -> Optional[str]:
    """Return the substring of the first balanced ``[...]`` in ``text``,
    or None if no balanced array is present. Tolerates nested arrays
    (e.g. an array of arrays) by tracking bracket depth and string
    state. Used to pull a JSON array out of a CoT blob like:
    ``"Let me think... The answer is [\\\\\"neutral\\\\\", \\\"alarmist\\\\"]."``
    """
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _validate_tone_list(data: list, expected_len: int) -> Optional[list[str]]:
    """Length check + per-item tone whitelist. Returns the cleaned list
    of lowercase labels, or None on any mismatch.
    """
    if len(data) != expected_len:
        return None
    out: list[str] = []
    for item in data:
        label = str(item).strip().lower()
        if label not in _VALID_TONES:
            return None
        out.append(label)
    return out


async def _classify_tones(titles: list[str]) -> Optional[list[str]]:
    """One LLM call classifying every title in ``titles`` at once.
    Returns None if no provider is configured or every provider
    fails/returns unusable output — callers leave framing_tone NULL
    and pick it back up on the next run."""
    if not titles:
        return []
    providers = router.providers_for("scoring")
    if not providers:
        logger.info("framing: no LLM provider configured for tone tagging — skipping")
        return None
    prompt = _build_tone_prompt(titles)
    for candidate in providers:
        try:
            content = await candidate.complete(prompt, max_tokens=_TONE_MAX_TOKENS)
        except ProviderError as exc:
            logger.warning("framing: tone call failed on %s: %s — trying next provider", candidate.name, exc)
            continue
        parsed = _parse_tone_response(content, len(titles))
        if parsed is not None:
            return parsed
        logger.warning("framing: %s returned unparsable tone output, trying next provider", candidate.name)
    logger.warning("framing: all configured LLM providers failed or returned unusable tone output")
    return None


async def _tag_untagged_clusters(session: AsyncSession) -> int:
    """For every cluster with at least one untagged member, fire ONE
    batched LLM call covering all of that cluster's untagged
    headlines. Returns the number of LLM calls made (0 if nothing was
    untagged or no provider is configured)."""
    rows = (
        await session.execute(
            select(Entry.id, Entry.title, Entry.story_cluster_id).where(
                Entry.story_cluster_id.isnot(None), Entry.framing_tone.is_(None)
            )
        )
    ).all()
    if not rows:
        return 0

    by_cluster: dict[int, list] = defaultdict(list)
    for r in rows:
        by_cluster[r.story_cluster_id].append(r)

    calls = 0
    for cluster_id, members in by_cluster.items():
        titles = [m.title for m in members]
        labels = await _classify_tones(titles)
        calls += 1
        if labels is None:
            continue
        for member, label in zip(members, labels):
            await session.execute(
                update(Entry).where(Entry.id == member.id).values(framing_tone=label)
            )
        await session.commit()
    return calls

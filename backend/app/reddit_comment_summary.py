"""Reddit thread comment-discussion LLM summarization.

Pairs with ``app.reddit_client.fetch_thread_comments`` — that
function fetches + parses a thread's comments on demand (the user
tapping "summarize comments" on a card), this one turns them into a
short discussion summary via the same LLM provider chain
``app.article_summary`` and ``app.podcast_transcript`` use.
"""

from __future__ import annotations

import asyncio
import logging

from app.llm import ProviderError, router

logger = logging.getLogger("popping.reddit_comment_summary")

_SUMMARY_MAX_TOKENS = 200
_COMMENTS_CHAR_BUDGET = 12_000

# Same reasoning as app.article_summary._LLM_CALL_TIMEOUT_S — this is
# a tap-and-wait UI affordance, not a background job, so it needs a
# much tighter bound than the provider clients' own timeouts (up to
# 120s for Ollama Cloud).
_LLM_CALL_TIMEOUT_S = 20.0


def _build_prompt(title: str, comments: list[dict]) -> str:
    lines = []
    total = 0
    for c in comments:
        line = f"{c['author']}: {c['text']}"
        total += len(line)
        if total > _COMMENTS_CHAR_BUDGET:
            break
        lines.append(line)
    thread_text = "\n".join(lines)
    return (
        "Write a summary in 3-4 sentences of plain prose (no headers, "
        "no bullet lists) of the following Reddit comment discussion. "
        "Cover the main opinions, points of agreement or disagreement, "
        "and any especially highly-discussed points. Do not "
        "editorialize beyond what's in the comments, and do not "
        f"mention that you are summarizing.\n\nThread: {title}\n\n"
        f"Comments:\n{thread_text}"
    )


async def summarize_comments(title: str, comments: list[dict]) -> str | None:
    """Summarize ``comments`` (as returned by
    ``app.reddit_client.fetch_thread_comments``) via the configured
    LLM provider chain. Returns None if no provider is configured,
    ``comments`` is empty, or every configured provider fails — same
    contract as ``app.article_summary.summarize_article`` and
    ``app.podcast_transcript.summarize_transcript``."""
    if not comments:
        return None
    providers = router.providers_for("brief")
    if not providers:
        logger.info("reddit_comment_summary: no LLM provider configured — skipping")
        return None
    prompt = _build_prompt(title, comments)
    for candidate in providers:
        try:
            content = await asyncio.wait_for(
                candidate.complete(prompt, max_tokens=_SUMMARY_MAX_TOKENS),
                timeout=_LLM_CALL_TIMEOUT_S,
            )
        except ProviderError as exc:
            logger.warning(
                "reddit_comment_summary: LLM call failed on %s: %s — trying next provider",
                candidate.name, exc,
            )
            continue
        except asyncio.TimeoutError:
            logger.warning(
                "reddit_comment_summary: %s took longer than %.0fs — trying next provider",
                candidate.name, _LLM_CALL_TIMEOUT_S,
            )
            continue
        content = (content or "").strip()
        if content:
            return content
    logger.warning("reddit_comment_summary: all configured LLM providers failed")
    return None

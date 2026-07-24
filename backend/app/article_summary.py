"""Article full-text LLM summarization.

Pairs with ``app.article_extract`` — that module fetches + extracts
the article's readable text on demand (the user expanding a card),
this one turns it into a 3-4 sentence summary via the same LLM
provider chain the Brief generator and podcast-transcript summarizer
use (``app.llm.router.providers_for("brief")``). See
``routes/entries.py``'s ``entry_summary_endpoint`` for how this
composes with the older, LLM-free feed-blurb extraction as a
fallback when no provider is configured or the article fetch fails.
"""

from __future__ import annotations

import logging

from app.llm import ProviderError, router

logger = logging.getLogger("popping.article_summary")

# 3-4 sentences of plain prose is comfortably under 150 tokens for
# every model in the provider chain; generous headroom in case a
# model runs verbose.
_SUMMARY_MAX_TOKENS = 200


def _build_prompt(title: str, article_text: str) -> str:
    return (
        "Write a summary in exactly 3-4 sentences of plain prose (no "
        "headers, no bullet lists) of the following news article. "
        "Cover the main point and the most important supporting "
        "details. Do not editorialize or add information not present "
        "in the article, and do not mention that you are "
        f"summarizing.\n\nHeadline: {title}\n\nArticle:\n{article_text}"
    )


async def summarize_article(title: str, article_text: str) -> str | None:
    """Summarize ``article_text`` via the configured LLM provider chain.
    Returns None if no provider is configured or every configured
    provider fails — same contract as
    ``app.podcast_transcript.summarize_transcript``."""
    providers = router.providers_for("brief")
    if not providers:
        logger.info("article_summary: no LLM provider configured — skipping")
        return None
    prompt = _build_prompt(title, article_text)
    for candidate in providers:
        try:
            content = await candidate.complete(prompt, max_tokens=_SUMMARY_MAX_TOKENS)
        except ProviderError as exc:
            logger.warning(
                "article_summary: LLM call failed on %s: %s — trying next provider",
                candidate.name, exc,
            )
            continue
        content = (content or "").strip()
        if content:
            return content
    logger.warning("article_summary: all configured LLM providers failed")
    return None

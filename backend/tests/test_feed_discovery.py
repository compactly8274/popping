"""Tests for the LLM-response parser in app.feed_discovery.

The thinking-model fix mirrors ``app.framing._parse_tone_response``:
two-stage parser that tries ``json.loads`` on the whole text first
(clean responses, non-thinking models) and falls back to a
bracket-extractor for chain-of-thought blobs (gpt-oss,
deepseek-r1, glm-5.2, ...).

The bracket-extractor is the part that breaks without these tests:
a naive ``re.findall(r"\\[.*?\\]")`` would stop at the first
``]`` inside a string literal. The hand-rolled scanner tracks
bracket depth AND string state. This file covers the realistic
CoT shapes an LLM might produce.

All tests are pure-function — no DB, no httpx, no LLM.
"""
from __future__ import annotations

import os
import sys
import tempfile
from typing import Any

os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_USER", "x")
os.environ.setdefault("POSTGRES_PASSWORD", "x")
os.environ.setdefault("POSTGRES_DB", "x")
os.environ.setdefault("EMBEDDING_ENABLED", "false")
os.environ.setdefault("ASSETS_DIR", tempfile.mkdtemp(prefix="smoke-"))

sys.path.insert(0, "/tmp/popping-review/backend")

import pytest

from app.feed_discovery import _extract_first_json_array, _try_parse_suggestion_list


# --- _extract_first_json_array ------------------------------------------

class TestExtractFirstJsonArray:
    def test_returns_balanced_array(self) -> None:
        text = 'reasoning here [1, 2, 3] and more text'
        assert _extract_first_json_array(text) == "[1, 2, 3]"

    def test_handles_quote_inside_string(self) -> None:
        # The whole point of the hand-rolled scanner. A naive
        # regex would stop at the first ] inside the string.
        text = '["a]b", "c"]'
        result = _extract_first_json_array(text)
        assert result == '["a]b", "c"]'

    def test_handles_escaped_quote(self) -> None:
        text = r'["a\"b", "c"]'
        result = _extract_first_json_array(text)
        assert result == r'["a\"b", "c"]'

    def test_handles_nested_arrays(self) -> None:
        text = '[[1, 2], [3, 4]]'
        assert _extract_first_json_array(text) == "[[1, 2], [3, 4]]"

    def test_handles_object_in_array(self) -> None:
        # The real-world feed_discovery shape: each element is a
        # dict with name/url/blurb.
        text = 'reasoning [{"name": "x", "url": "https://x", "blurb": "y"}]'
        result = _extract_first_json_array(text)
        assert result is not None
        assert '"name": "x"' in result
        assert '"url": "https://x"' in result

    def test_returns_none_when_no_array(self) -> None:
        assert _extract_first_json_array("just plain text") is None

    def test_returns_none_for_unbalanced(self) -> None:
        # Never finds a balanced [ ... ]. Trailing text after the
        # unclosed bracket means the depth never returns to 0.
        assert _extract_first_json_array("[1, 2, 3") is None

    def test_returns_none_for_empty_input(self) -> None:
        assert _extract_first_json_array("") is None

    def test_handles_multiple_arrays_takes_first(self) -> None:
        text = "first [1, 2] then [3, 4]"
        assert _extract_first_json_array(text) == "[1, 2]"


# --- _try_parse_suggestion_list -----------------------------------------

class TestTryParseSuggestionList:
    def test_clean_json_array(self) -> None:
        text = '[{"name": "x", "url": "https://x", "blurb": "y"}]'
        result = _try_parse_suggestion_list(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "x"

    def test_empty_array(self) -> None:
        text = "[]"
        result = _try_parse_suggestion_list(text)
        assert result == []

    def test_dict_wrapped_in_object(self) -> None:
        # Some providers wrap the array in {"feeds": [...]} despite
        # the prompt.
        text = '{"feeds": [{"name": "x", "url": "https://x", "blurb": "y"}]}'
        result = _try_parse_suggestion_list(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "x"

    def test_dict_wrapped_in_suggestions(self) -> None:
        text = '{"suggestions": [{"name": "x", "url": "https://x", "blurb": "y"}]}'
        result = _try_parse_suggestion_list(text)
        assert result is not None
        assert len(result) == 1

    def test_code_fenced_json(self) -> None:
        # The code-fence stripper in _ask_llm_for_suggestions runs
        # before this. _try_parse_suggestion_list itself doesn't
        # strip — the caller does. But verify it parses what
        # passes through.
        text = '[{"name": "x", "url": "https://x", "blurb": "y"}]'
        result = _try_parse_suggestion_list(text)
        assert result is not None

    def test_cot_with_trailing_array(self) -> None:
        # The real bug: thinking model returns CoT then a JSON
        # array at the end. Without the bracket-extractor, the
        # whole text fails to parse and the call is treated as
        # "non-JSON output".
        text = (
            "Let me think about this. The user is in the deals "
            "category. I should suggest feeds that are similar. "
            "Let me consider: slickdeals, dealnews, etc.\n\n"
            'The answer is [{"name": "slickdeals", "url": "https://slickdeals.net/feed.xml", "blurb": "Daily deals"}]'
        )
        result = _try_parse_suggestion_list(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "slickdeals"

    def test_cot_with_nested_quote(self) -> None:
        # The CoT contains a quote inside a string literal —
        # the same edge case the framing.py tests cover.
        text = (
            "Reasoning: I should look at feeds that cover this. "
            "Like [\"foo's deals\", \"bar's best\"]...\n\n"
            "Wait let me reconsider. The answer is:\n"
            '[{"name": "foo", "url": "https://foo.com/feed", "blurb": "Foo\'s deals"}]'
        )
        result = _try_parse_suggestion_list(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "foo"

    def test_returns_none_for_garbage(self) -> None:
        # No JSON anywhere — should return None, not raise.
        result = _try_parse_suggestion_list(
            "I cannot answer this question. There are no real feeds."
        )
        assert result is None

    def test_returns_none_for_scalar(self) -> None:
        # LLM returned a number, not a list. The whole-text parse
        # succeeds but the result is not a list; the bracket
        # extractor finds nothing; return None.
        result = _try_parse_suggestion_list("5")
        assert result is None

    def test_returns_none_for_unbalanced_cot(self) -> None:
        # CoT that mentions a [ but never closes it. The
        # bracket-extractor should return None, not loop.
        result = _try_parse_suggestion_list(
            "Reasoning: I'm looking for feeds [unclosed here"
        )
        assert result is None

    def test_handles_multiple_arrays_in_cot_picks_list_of_dicts(self) -> None:
        # The framing.py parser stops at the first balanced
        # array, which works for it (one list of strings per
        # call). feed_discovery wants a list of dicts, so the
        # parser here scans ALL balanced arrays and picks the
        # first one whose contents are dicts — the CoT's
        # bracketed strings (a list of strings) is ignored.
        text = (
            'Let me think: ["first thought", "second"] '
            "Then the answer: "
            '[{"name": "x", "url": "https://x", "blurb": "y"}]'
        )
        result = _try_parse_suggestion_list(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "x"


# --- Integration: full _ask_llm_for_suggestions with mock --------------

class TestAskLlmForSuggestions:
    """Verify the full code path: provider returns text, _ask_llm
    parses it, returns the right tuple. The provider is mocked
    at the LLM provider-chain level so we don't need any
    real LLM."""

    def test_cot_blurb_parses_via_bracket_extractor(self) -> None:
        # End-to-end: a thinking model returns CoT + JSON array.
        # The provider is mocked to return that as its text.
        from unittest.mock import AsyncMock, patch
        from app.feed_discovery import _ask_llm_for_suggestions

        cot_response = (
            "Let me think about this. The user is in the tech "
            "category. I should suggest RSS feeds that cover "
            "technology news.\n\n"
            'The answer is [{"name": "ars_technica", "url": "https://feeds.arstechnica.com/arstechnica/index", "blurb": "Technology news and analysis"}]'
        )

        # Mock provider: .complete returns the CoT text, .name is
        # the label, model is on _THINKING_MODELS so the
        # OllamaCloudProvider fallback would have already
        # substituted the thinking field. We're testing the
        # _ask_llm_for_suggestions parser, not the provider
        # substitution — so just return the final content
        # directly here.
        mock_provider = AsyncMock()
        mock_provider.name = "mock"
        mock_provider.complete = AsyncMock(return_value=cot_response)

        with patch("app.feed_discovery.router") as mock_router:
            mock_router.providers_for = lambda task: [mock_provider]
            suggestions, note = asyncio_run(
                _ask_llm_for_suggestions(
                    category="tech",
                    context="user just added a feed",
                    exclude_names=set(),
                    limit=5,
                )
            )

        assert note is None
        assert len(suggestions) == 1
        assert suggestions[0]["name"] == "ars_technica"
        assert "arstechnica" in suggestions[0]["url"]


def asyncio_run(coro):
    """Helper for the single async integration test."""
    import asyncio
    return asyncio.run(coro)

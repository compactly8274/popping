"""probe() (the Add/Test preview) must give up on a slow site instead of
spinning for minutes, and keep whatever it extracted before the cap."""

import asyncio

import pytest

from app.article_detect import Candidate
from app.sources import generic_scrape


def _plan(urls):
    """A plan with these candidates, none stored (``plan_candidates``)."""
    candidates = [Candidate(url, None, None) for url in urls]
    return generic_scrape.Plan("sitemap", candidates, candidates)


@pytest.mark.asyncio
async def test_probe_returns_partial_results_at_budget(monkeypatch):
    async def fake_candidates(url, *args, **kwargs):
        return _plan(f"https://example.com/news/{i}" for i in range(10))

    async def fake_extract(url, custom_headers=None, **kwargs):
        if url.endswith("/0"):
            return {"url": url, "title": "fast"}
        await asyncio.sleep(60)

    monkeypatch.setattr(generic_scrape, "plan_candidates", fake_candidates)
    monkeypatch.setattr(generic_scrape, "_extract_one", fake_extract)
    monkeypatch.setattr(generic_scrape, "_PROBE_BUDGET", 0.2)

    items = await asyncio.wait_for(generic_scrape.probe("https://example.com/news/"), 5)
    assert items == []  # the first chunk never finished, so nothing landed


@pytest.mark.asyncio
async def test_probe_stops_at_limit(monkeypatch):
    calls = []

    async def fake_candidates(url, *args, **kwargs):
        return _plan(f"https://example.com/news/{i}" for i in range(10))

    async def fake_extract(url, custom_headers=None, **kwargs):
        calls.append(url)
        return {"url": url, "title": url}

    monkeypatch.setattr(generic_scrape, "plan_candidates", fake_candidates)
    monkeypatch.setattr(generic_scrape, "_extract_one", fake_extract)

    items = await generic_scrape.probe("https://example.com/news/", limit=3)
    assert [i["url"] for i in items] == [f"https://example.com/news/{i}" for i in range(3)]
    assert len(calls) == 3

"""GitHub releases for a small set of repositories.

Pulls `/repos/{owner}/{repo}/releases` per repo, then merges and
sorts by ``published_at`` DESC. Honors GitHub's ETag / 304 responses
to stay well under the 60-req/hr unauthenticated budget:

  - 5 repos × 1 req per refresh × 2 refreshes per hour = 10 req/hr
  - with ETag, only the first run after a release publishes a non-304

If ``GITHUB_TOKEN`` is set in env, requests include it as
``Authorization: Bearer …`` — raises the limit to 5000/hr for users
who watch many repos.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

import httpx

from app.config import settings
from app.sources import register_source
from app.sources.base import SourcePlugin

logger = logging.getLogger("popping.sources.github_releases")

_BASE = "https://api.github.com"
_TIMEOUT = 15.0
_TOP_PER_REPO = 5  # only the most recent N releases per repo make the feed

# Default repo set. Operators can fork and tweak the source plugin to
# change it; we don't expose this through the API in phase 3.
_DEFAULT_REPOS = [
    "python/cpython",
    "nodejs/node",
    "kubernetes/kubernetes",
    "rust-lang/rust",
    "kubernetes/ingress-nginx",
]

# ETag cache — keyed by repo name. Lives for the process lifetime;
# process restart re-warms (small price for not having to persist).
_etag_cache: dict[str, str] = {}


def _gh_headers() -> dict[str, str]:
    h = {
        "User-Agent": "Popping/0.2 (+https://github.com/compactly8274/popping)",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_token:
        h["Authorization"] = f"Bearer {settings.github_token}"
    return h


def _parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        # GitHub returns ISO 8601 with a trailing 'Z' for UTC.
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _fetch_repo(
    client: httpx.AsyncClient,
    repo: str,
) -> list[dict]:
    url = f"{_BASE}/repos/{repo}/releases?per_page={_TOP_PER_REPO}"
    headers = _gh_headers()
    if (etag := _etag_cache.get(repo)):
        headers["If-None-Match"] = etag
    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("github_releases: %s transport error: %s", repo, exc)
        return []
    if resp.status_code == 304:
        logger.debug("github_releases: %s not modified (304)", repo)
        return []
    if resp.status_code != 200:
        # Rate limit (403 with x-ratelimit-remaining=0) is the most
        # common. Log it once and back off; the next refresh will try
        # again.
        logger.warning(
            "github_releases: %s returned %d: %s",
            repo,
            resp.status_code,
            resp.text[:200],
        )
        return []
    # Stash the new ETag for next time.
    if (etag := resp.headers.get("ETag")):
        _etag_cache[repo] = etag
    items: list[dict] = []
    for rel in resp.json():
        title = rel.get("name") or rel.get("tag_name") or repo
        # Some repos have no body — fall back to the tag.
        body = rel.get("body") or ""
        url_value = rel.get("html_url") or f"https://github.com/{repo}/releases"
        items.append(
            {
                "title": f"{repo.split('/')[-1]} {rel.get('tag_name', '')}: {title}".strip(),
                "url": url_value,
                "published_at": _parse_iso(rel.get("published_at")),
                "summary": body,
                "meta": {
                    "repo": repo,
                    "tag": rel.get("tag_name"),
                    "prerelease": rel.get("prerelease", False),
                    "author": (rel.get("author") or {}).get("login"),
                },
            }
        )
    return items


@register_source
class GithubReleases(SourcePlugin):
    name = "github_releases"
    type = "api"
    category = "tech"
    url = f"{_BASE}/repos/python/cpython/releases"  # canonical "primary" repo
    refresh_interval_seconds = 1800  # 30 min

    repos: list[str] = _DEFAULT_REPOS  # class-level so tests can override

    async def fetch(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            # Concurrent across repos — keeps total wall time at one
            # round-trip. 5 in flight is fine for our default list.
            per_repo = await asyncio.gather(
                *(_fetch_repo(client, repo) for repo in self.repos),
                return_exceptions=False,
            )
        merged: list[dict] = []
        for chunk in per_repo:
            merged.extend(chunk)
        # Sort newest first so the embeddings ingest in the same order
        # they'd appear in the feed (recency-decay then sees them at
        # top_score first).
        merged.sort(
            key=lambda r: r.get("published_at") or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            reverse=True,
        )
        return merged
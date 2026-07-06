"""Small row-builders shared across integration tests.

Not a full factory library — just enough to keep each test's ``arrange``
step to one or two lines instead of repeating every required column.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entry, Source

_next_url_id = 0


def _unique_suffix() -> int:
    global _next_url_id
    _next_url_id += 1
    return _next_url_id


async def make_source(
    session: AsyncSession,
    name: str,
    *,
    category: str = "tech",
    url: str | None = None,
    type: str = "rss",
    active: bool = True,
    error_count: int = 0,
    last_error: str | None = None,
    refresh_interval_seconds: int = 3600,
) -> Source:
    source = Source(
        name=name,
        type=type,
        category=category,
        url=url or f"https://example.com/{name}.xml",
        active=active,
        error_count=error_count,
        last_error=last_error,
        refresh_interval_seconds=refresh_interval_seconds,
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)
    return source


async def make_entry(
    session: AsyncSession,
    source: Source,
    title: str,
    *,
    published_at: dt.datetime | None = None,
    fetched_at: dt.datetime | None = None,
    composite_score: float = 0.0,
    personal_score: float = 0.0,
    raw_score: float = 0.0,
    embedding: list[float] | None = None,
    url: str | None = None,
) -> Entry:
    entry = Entry(
        source_id=source.id,
        title=title,
        url=url or f"https://example.com/entry/{_unique_suffix()}",
        published_at=published_at or dt.datetime.now(dt.timezone.utc),
        composite_score=composite_score,
        personal_score=personal_score,
        raw_score=raw_score,
        embedding=embedding,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    if fetched_at is not None:
        # fetched_at has a server_default; overwrite it directly for
        # tests that need to simulate "ingested N days ago".
        entry.fetched_at = fetched_at
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
    return entry

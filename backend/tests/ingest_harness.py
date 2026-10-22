"""Shared scaffolding for tests that run ``scheduler._ingest`` end to end
against the test database: a fake feed plugin, stubs for the embedder
and the network asset passes, and helpers that move a feed's clock back so "3 days later" or "after
21 days away" can be set up without waiting."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import func, select, text

from app import scheduler
from app.models import Entry, Source
from app.sources.base import SourcePlugin


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def item(n: int, *, hours_ago: Optional[float], host: str = "feed.test") -> dict:
    """One raw feed item. ``hours_ago=None`` makes it undated."""
    when = None if hours_ago is None else utcnow() - dt.timedelta(hours=hours_ago)
    return {"title": f"Story {n}", "url": f"https://{host}/{n}", "published_at": when}


def make_plugin(name: str, *, category: str = "news") -> type[SourcePlugin]:
    """A class-driven plugin whose ``fetch`` returns ``cls.items``."""

    class _Plugin(SourcePlugin):
        type = "rss"
        url = f"https://{name}.test/feed"
        items: list[dict] = []

        async def fetch(self) -> list[dict]:
            return list(type(self).items)

    _Plugin.name = name
    _Plugin.category = category
    return _Plugin


def install_offline_ingest(monkeypatch) -> list[str]:
    """Skip the embedder, the job rescheduling and the network asset
    passes (favicon, thumbnails, og:image). Returns the list of URLs
    that reached the embed step (one per item the rules kept). Tests
    use it through the ``offline_ingest`` fixture in ``conftest.py``."""
    calls: list[str] = []

    async def fake_embed(norm):
        calls.append(norm["url"])
        return None

    async def no_asset(*args, **kwargs):
        return None

    async def no_favicon(*args, **kwargs):
        return None, None

    monkeypatch.setattr(scheduler, "_embed_text", fake_embed)
    monkeypatch.setattr(scheduler, "_reschedule_ingest_job", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.assets, "fetch_favicon", no_favicon)
    monkeypatch.setattr(scheduler.assets, "fetch_thumbnail", no_asset)
    monkeypatch.setattr(scheduler.assets, "fetch_og_image_fallback", no_asset)
    return calls


def accounted(summary: dict) -> bool:
    """Every fetched item is inserted, a duplicate or skipped."""
    return summary["fetched"] == summary["inserted"] + summary["duplicates"] + summary["skipped"]


async def source_row(session, name: str) -> Source:
    session.expire_all()
    return await session.scalar(select(Source).where(Source.name == name))


async def entry_count(session, name: str) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(Entry)
        .join(Source, Entry.source_id == Source.id)
        .where(Source.name == name)
    )


async def age_feed(session, name: str, *, hours: float, last_fetch_hours: Optional[float] = None) -> None:
    """Move a feed's past back by ``hours``: its baseline (``created_at``),
    its stored rows' ``fetched_at`` and, unless ``last_fetch_hours`` says
    otherwise, its ``last_fetch_at``. ``last_fetch_hours`` sets the last
    success to that many hours ago instead (an hourly feed that was
    added long ago but polled recently)."""
    delta = dt.timedelta(hours=hours).total_seconds()
    source = await source_row(session, name)
    await session.execute(
        text("UPDATE entries SET fetched_at = fetched_at - make_interval(secs => :d) WHERE source_id = :id"),
        {"d": delta, "id": source.id},
    )
    await session.execute(
        text("UPDATE sources SET created_at = created_at - make_interval(secs => :d) WHERE id = :id"),
        {"d": delta, "id": source.id},
    )
    if last_fetch_hours is not None:
        await session.execute(
            text("UPDATE sources SET last_fetch_at = :t WHERE id = :id"),
            {"t": utcnow() - dt.timedelta(hours=last_fetch_hours), "id": source.id},
        )
    else:
        await session.execute(
            text(
                "UPDATE sources SET last_fetch_at = last_fetch_at - make_interval(secs => :d) "
                "WHERE id = :id AND last_fetch_at IS NOT NULL"
            ),
            {"d": delta, "id": source.id},
        )
    await session.commit()

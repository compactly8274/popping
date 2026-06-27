"""Entry listing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Entry, Source
from app.schemas import EntryOut

router = APIRouter(tags=["entries"])


@router.get("/entries", response_model=list[EntryOut])
async def list_entries(
    session: AsyncSession = Depends(get_session),
    category: str | None = Query(default=None, description="Filter by source category"),
    source: str | None = Query(default=None, description="Filter by source name"),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[EntryOut]:
    stmt = (
        select(Entry)
        .join(Source, Entry.source_id == Source.id)
        .order_by(Entry.composite_score.desc(), Entry.published_at.desc().nullslast())
        .limit(limit)
    )
    if category:
        stmt = stmt.where(Source.category == category)
    if source:
        stmt = stmt.where(Source.name == source)
    rows = (await session.scalars(stmt)).all()
    return [EntryOut.model_validate(r) for r in rows]

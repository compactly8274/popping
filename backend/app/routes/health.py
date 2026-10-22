"""Health check endpoint."""

from __future__ import annotations


from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Entry, Source
from app.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(
    session: AsyncSession = Depends(get_session),
) -> HealthOut:
    db_ok = "ok"
    sources_count = 0
    entries_count = 0
    last_fetch = None

    try:
        sources_count = await session.scalar(select(func.count()).select_from(Source)) or 0
        entries_count = await session.scalar(select(func.count()).select_from(Entry)) or 0
        last = await session.scalar(
            select(Source.last_fetch_at).order_by(Source.last_fetch_at.desc().nullslast()).limit(1)
        )
        last_fetch = last
    except Exception:
        db_ok = "error"

    overall = "ok" if db_ok == "ok" else "degraded"
    return HealthOut(
        status=overall,
        sources=int(sources_count),
        entries=int(entries_count),
        db=db_ok,
        last_fetch=last_fetch,
    )
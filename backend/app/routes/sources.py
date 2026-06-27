"""Source listing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Source
from app.schemas import SourceOut

router = APIRouter(tags=["sources"])


@router.get("/sources", response_model=list[SourceOut])
async def list_sources_endpoint(
    session: AsyncSession = Depends(get_session),
) -> list[SourceOut]:
    rows = (await session.scalars(select(Source).order_by(Source.category, Source.name))).all()
    return [SourceOut.model_validate(r) for r in rows]


@router.get("/sources/{source_id}", response_model=SourceOut)
async def get_source_endpoint(
    source_id: int,
    session: AsyncSession = Depends(get_session),
) -> SourceOut:
    row = await session.get(Source, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    return SourceOut.model_validate(row)

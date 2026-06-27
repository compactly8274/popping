"""Manual ingest trigger endpoint.

Lets the UI / curl force a single source to fetch right now instead of
waiting for its scheduler tick. Useful for cold-start demos and debugging.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas import IngestResult
from app.scheduler import trigger_now

router = APIRouter(tags=["ingest"])


@router.post("/ingest/{source_name}", response_model=IngestResult)
async def ingest_endpoint(source_name: str) -> IngestResult:
    summary = await trigger_now(source_name)
    return IngestResult(**summary)
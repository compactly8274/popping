"""Manual ingest trigger endpoint.

Lets the UI / curl force a single source to fetch right now instead of
waiting for its scheduler tick. Useful for cold-start demos and debugging.

Auth: when OIDC is enabled, requires a logged-in user. Scheduler-driven
fetches (in app.scheduler) are server-side and skip this gate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.auth.deps import require_user
from app.config import settings
from app.schemas import IngestResult
from app.scheduler import trigger_now
from app.sources import list_sources

# ``dependencies`` is evaluated at import time. When OIDC is disabled we
# pass an empty list and the endpoint stays wide-open — matches phase 1.
_route_deps = [Depends(require_user)] if settings.oidc_enabled else []

router = APIRouter(tags=["ingest"], dependencies=_route_deps)


@router.post("/ingest/{source_name}", response_model=IngestResult)
async def ingest_endpoint(source_name: str) -> IngestResult:
    # 404 only if the name is neither a registered plugin class nor
    # a dynamic ``Source`` row. ``trigger_now`` already handles
    # both cases (registry hit goes to the in-memory class; DB hit
    # goes through ``_plugin_for(row)`` and then ``_ingest``);
    # we just need to pre-check the dynamic case so the 404
    # message stays meaningful. Without the DB lookup, a dynamic
    # source would 404 even though ``trigger_now`` would handle it
    # fine — a confusing user-facing bug ("I just added this
    # source and it can't even fetch it").
    if source_name in list_sources():
        pass
    else:
        from app.db import SessionLocal
        from app.models import Source

        async with SessionLocal() as session:
            row = await session.scalar(
                select(Source).where(Source.name == source_name)
            )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="unknown source",
            )
    summary = await trigger_now(source_name)
    return IngestResult(**summary)
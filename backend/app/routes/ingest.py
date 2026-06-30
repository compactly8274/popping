"""Manual ingest trigger endpoint.

Lets the UI / curl force a single source to fetch right now instead of
waiting for its scheduler tick. Useful for cold-start demos and debugging.

Auth: when OIDC is enabled, requires a logged-in user. Scheduler-driven
fetches (in app.scheduler) are server-side and skip this gate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

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
    # 404 on unknown source. ``trigger_now`` itself returns an
    # ``error``-bearing 200 for unknown plugins; we want a proper
    # 404 so the UI can render "source not found" distinctly from
    # "the fetch failed". Inside the scheduler, the registered
    # plugin and any dynamic rows are both keyed by ``name``, so a
    # missing entry is a real "not found" — not a transient state.
    if source_name not in list_sources():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown source",
        )
    summary = await trigger_now(source_name)
    return IngestResult(**summary)
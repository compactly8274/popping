"""Brief endpoints.

  GET  /api/brief/latest   — most recent Brief for a given tone (or all tones).
  POST /api/brief/generate — manually trigger generation (login-gated).
  GET  /api/notifications/status — Drawer chip status (no secrets).

Manual generation runs in the request path; the LLM call takes a few
seconds on Ollama, ~1-2 s on Anthropic/OpenAI. That's an acceptable
UX for a button click.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_user
from app.brief import BriefGenerator
from app.config import settings
from app.db import SessionLocal, get_session
from app.models import Brief
from app.notify import notifier_status
from app.request_state import current_notifier
from app.schemas import BriefOut, NotificationStatus

logger = logging.getLogger("popping.routes.brief")

_route_deps = [Depends(require_user)] if settings.oidc_enabled else []

router = APIRouter(tags=["brief"])


@router.get("/brief/latest", response_model=list[BriefOut])
async def brief_latest(
    session: AsyncSession = Depends(get_session),
    tone: str | None = Query(default=None, description="Filter by tone; omit for latest of each tone"),
    limit: int = Query(default=5, ge=1, le=20),
) -> list[BriefOut]:
    """Most-recent Briefs. With ``tone`` set, returns up to ``limit``
    matching that tone; with it omitted, returns the latest Brief for
    each tone (terse / narrative / alert)."""
    stmt = select(Brief).order_by(desc(Brief.generated_at)).limit(limit * 4)
    if tone:
        stmt = stmt.where(Brief.tone == tone)
    rows = (await session.scalars(stmt)).all()

    if tone:
        return [BriefOut.model_validate(r) for r in rows[:limit]]

    # No tone filter — return at most one per tone (the latest).
    seen: dict[str, Brief] = {}
    for row in rows:
        if row.tone not in seen:
            seen[row.tone] = row
    out = sorted(seen.values(), key=lambda b: b.generated_at, reverse=True)
    return [BriefOut.model_validate(b) for b in out]


@router.post(
    "/brief/generate",
    response_model=BriefOut,
    dependencies=_route_deps,
)
async def brief_generate(
    tone: str = Query(default="terse", description="terse | narrative"),
) -> BriefOut:
    """Synchronously generate a new Brief. Returns the persisted row.

    Use the dedup of the LLM router — running this twice on the same
    day just produces two rows (the latest wins in the UI). We don't
    coalesce here; the operator might want to compare two runs.
    """
    notifier = current_notifier()
    gen = BriefGenerator(notifier)
    # Open a fresh session so the request task can write + dispatch
    # in one transaction.
    async with SessionLocal() as session:
        try:
            brief = await gen.generate(session=session, tone=tone)
        except Exception:
            logger.exception("brief generate failed")
            raise HTTPException(status_code=500, detail="brief generation failed")
        if brief is None:
            raise HTTPException(
                status_code=503,
                detail=BriefGenerator.skip_reason(),
            )
        await session.commit()
        await session.refresh(brief)
        return BriefOut.model_validate(brief)


@router.get("/notifications/status", response_model=NotificationStatus)
async def notifications_status() -> NotificationStatus:
    """Drawer chip — does the backend have a working notifier? No secrets."""
    return NotificationStatus(**notifier_status())
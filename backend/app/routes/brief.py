"""Brief endpoints.

  GET  /api/brief/latest        — most recent Brief for a given tone (or all tones).
  POST /api/brief/generate      — kick off generation; returns 202 + job id (login-gated).
  GET  /api/brief/jobs/{job_id} — poll a generation job. Terminal in completed/failed.
  GET  /api/notifications/status — Drawer chip status (no secrets).

Generation is async. The 202-on-kick path means the browser never
holds a connection open across an LLM roundtrip (3-10 s on Ollama).
``POST /api/brief/generate`` schedules the work, returns a UUID; the
BriefCard polls ``/api/brief/jobs/{id}`` every ~1s until the job
reaches a terminal state, then renders the result.

Job state lives in a process-local dict. Workers behind a load
balancer would need a Redis-backed ledger; until then (single-pod
deploys), the dict is fine and survives across requests without a DB
roundtrip.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_user
from app.brief import BriefGenerator
from app.config import settings
from app.db import SessionLocal, get_session
from app.llm import router as llm_router
from app.models import Brief
from app.notify import notifier_status
from app.request_state import current_notifier
from app.schemas import BriefOut, LLMStatus, NotificationStatus

logger = logging.getLogger("routes.brief")

# Require login on the write paths only; latest + chip reads are open
# even with OIDC enabled (matches the brief-latest endpoint's contract).
_route_deps = [Depends(require_user)] if settings.oidc_enabled else []

router = APIRouter(tags=["brief"])


# ---------------------------------------------------------------------------
# Brief generation jobs (process-local ledger)
# ---------------------------------------------------------------------------


class _Job(BaseModel):
    """Process-local record for an in-flight brief generation.

    Lives in ``_JOBS`` — not persisted to the DB. Survives across
    requests so the BriefCard's poll lands against the same UUID
    the POST returned. ``uvicorn --reload`` or a process restart
    drops the ledger; the job is then "lost". On a real deploy
    with multi-pod fan-in, swap this for a Redis-backed ledger —
    the public API contract doesn't change.
    """
    id: str
    tone: str
    status: str  # "pending" | "running" | "completed" | "failed"
    brief: Optional[BriefOut] = None
    error: Optional[str] = None
    started_at: float  # time.monotonic() — diagnostic only


_JOBS: dict[str, _Job] = {}
_JOBS_LOCK = asyncio.Lock()


# Cap the in-memory ledger so a runaway script can't OOM us. Stale
# completed/failed jobs from days-old sessions pile up if we never
# prune. Keep 200 recent jobs (covers a full day of Generate
# clicks); drop the rest on every insert.
_JOBS_MAX = 200


async def _record_job(job: _Job) -> None:
    async with _JOBS_LOCK:
        _JOBS[job.id] = job
        # Prune oldest entries when over cap. List values + sort by
        # ``started_at`` (ascending — oldest first) and trim. The
        # cap is generous so this rarely fires; the cost is linear
        # in the ledger size, fine for hundreds of entries.
        if len(_JOBS) > _JOBS_MAX:
            victims = sorted(_JOBS.values(), key=lambda j: j.started_at)[: len(_JOBS) - _JOBS_MAX]
            for v in victims:
                _JOBS.pop(v.id, None)


async def _run_job(job_id: str, tone: str) -> None:
    """Background task runner — produces the brief, updates the ledger,
    logs on failure. Exceptions are caught and translated into
    ``status="failed"`` so the polling endpoint returns something
    useful instead of hanging forever."""
    notifier = current_notifier()
    gen = BriefGenerator(notifier)
    async with SessionLocal() as session:
        try:
            brief = await gen.generate(session=session, tone=tone)
            if brief is None:
                # ``generate`` returns None for either "no provider" or
                # "no entries" or "LLM returned empty". Surface the
                # skip reason as the user-visible error message.
                async with _JOBS_LOCK:
                    job = _JOBS.get(job_id)
                    if job is not None:
                        job.status = "failed"
                        job.error = BriefGenerator.skip_reason()
                return
            await session.commit()
            await session.refresh(brief)
            async with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is not None:
                    job.status = "completed"
                    job.brief = BriefOut.model_validate(brief)
            logger.info("brief job: completed id=%s tone=%s brief_id=%d", job_id, tone, brief.id)
        except Exception:
            logger.exception("brief job: failed id=%s", job_id)
            async with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is not None:
                    job.status = "failed"
                    job.error = "brief generation failed (see backend logs)"


class BriefJobOut(BaseModel):
    id: str
    tone: str
    status: str
    brief: Optional[BriefOut] = None
    error: Optional[str] = None


class BriefGenerateAck(BaseModel):
    job_id: str
    tone: str


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
    response_model=BriefGenerateAck,
    status_code=202,
    dependencies=_route_deps,
)
async def brief_generate(
    tone: str = Query(
        default="terse",
        description="terse | narrative | alert (alert is auto-generated by source convergence)",
    ),
) -> BriefGenerateAck:
    """Kick off a new Brief generation. Returns 202 + a job id
    immediately; the BriefCard polls ``/api/brief/jobs/{job_id}``
    until the job reaches a terminal state, then renders the result.

    Why async? Generation takes 3-10 s on Ollama. Holding the
    request open across the LLM roundtrip means the browser sits
    with a pending connection (often hitting idle timeouts in
    reverse proxies); the 202 path means the user clicks Generate,
    sees the "thinking…" indicator in the card, and gets the brief
    when it lands. Browser-initiated double-clicks just spawn two
    jobs — both are bounded by the ledger cap.
    """
    job = _Job(
        id=str(uuid.uuid4()),
        tone=tone,
        status="running",
        started_at=time.monotonic(),
    )
    await _record_job(job)
    # Fire and forget. ``asyncio.create_task`` schedules on the
    # running loop; the task self-updates the ledger as it
    # progresses. If the loop is closing (process shutdown) the
    # task gets cancelled — fine, the brief was never persisted.
    asyncio.create_task(_run_job(job.id, tone))
    return BriefGenerateAck(job_id=job.id, tone=tone)


@router.get("/brief/jobs/{job_id}", response_model=BriefJobOut)
async def brief_job_status(job_id: str) -> BriefJobOut:
    """Poll for a generation job's status. Returns 404 if the
    job isn't (or no longer is) in the ledger; ``status`` is
    ``running`` | ``completed`` | ``failed`` for live jobs.

    The BriefCard polls this every ~1s while a Generate button
    is mid-flight. Once status is terminal, ``brief`` or
    ``error`` is populated and the card stops polling.
    """
    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        # Two possibilities: (a) the UUID was never issued,
        # (b) the job aged out of the ledger. Both surface as
        # 404; the BriefCard treats 404 as terminal (gives up).
        raise HTTPException(status_code=404, detail="brief job not found")
    return BriefJobOut(
        id=job.id,
        tone=job.tone,
        status=job.status,
        brief=job.brief,
        error=job.error,
    )


@router.get("/notifications/status", response_model=NotificationStatus)
async def notifications_status() -> NotificationStatus:
    """Drawer chip — does the backend have a working notifier? No secrets."""
    return NotificationStatus(**notifier_status())


@router.get("/llm/status", response_model=LLMStatus)
async def llm_status() -> LLMStatus:
    """Drawer chip — does the backend have a configured LLM? No secrets.
    Used by the Brief panel + the manual Generate button so the user can
    see which provider / model will be called before clicking."""
    return LLMStatus(**llm_router.status(task="brief"))
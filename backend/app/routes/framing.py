"""Framing Watch — same-story/different-headline clusters.

Read-only: clustering itself happens in the scheduler (``app.framing.
cluster_recent_entries``, see ``scheduler._cluster_framing``). This
route just serves whatever's currently in ``story_clusters`` +
``entries.story_cluster_id``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Entry, Source, StoryCluster
from app.schemas import FramingArticleOut, FramingClusterOut

router = APIRouter(tags=["framing"])


@router.get("/framing-clusters", response_model=list[FramingClusterOut])
async def framing_clusters_endpoint(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=100, description="Max clusters to return"),
) -> list[FramingClusterOut]:
    """Currently active Framing Watch clusters, most-recently-first-seen
    first. Each cluster has 2+ articles by construction — see
    ``app.framing.cluster_recent_entries``.

    One query: join entries -> sources -> story_clusters and order so
    rows for the same cluster land contiguously (cluster identity is
    the secondary sort key), then group in Python. Cheap at the row
    counts this feature produces (a handful of clusters, a few
    members each).
    """
    stmt = (
        select(
            Entry.id,
            Entry.title,
            Entry.url,
            Entry.published_at,
            Entry.framing_tone,
            Entry.story_cluster_id,
            Source.name.label("source_name"),
            Source.favicon_path,
            StoryCluster.wire_source,
            StoryCluster.first_seen_at,
        )
        .join(Source, Entry.source_id == Source.id)
        .join(StoryCluster, Entry.story_cluster_id == StoryCluster.id)
        .order_by(
            StoryCluster.first_seen_at.desc().nullslast(),
            Entry.story_cluster_id,
            Entry.published_at.asc().nullslast(),
        )
    )
    rows = (await session.execute(stmt)).all()

    clusters: dict[int, FramingClusterOut] = {}
    order: list[int] = []
    for r in rows:
        if r.story_cluster_id not in clusters:
            clusters[r.story_cluster_id] = FramingClusterOut(
                cluster_id=r.story_cluster_id,
                wire_source=r.wire_source,
                first_seen_at=r.first_seen_at,
                articles=[],
            )
            order.append(r.story_cluster_id)
        clusters[r.story_cluster_id].articles.append(
            FramingArticleOut(
                entry_id=r.id,
                title=r.title,
                url=r.url,
                source_name=r.source_name,
                favicon_path=r.favicon_path,
                published_at=r.published_at,
                framing_tone=r.framing_tone,
            )
        )

    return [clusters[cid] for cid in order[:limit]]

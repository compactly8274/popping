"""Pydantic request/response shapes."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel


class SourceOut(BaseModel):
    id: int
    name: str
    type: str
    category: str
    url: str
    refresh_interval_seconds: int
    last_fetch_at: Optional[dt.datetime]
    last_error: Optional[str]
    error_count: int
    active: bool

    class Config:
        from_attributes = True


class EntryOut(BaseModel):
    id: int
    source_id: int
    title: str
    url: str
    published_at: Optional[dt.datetime]
    composite_score: float
    personal_score: float
    raw_score: float
    meta: Optional[dict]

    class Config:
        from_attributes = True


class IngestResult(BaseModel):
    source: str
    fetched: int
    inserted: int
    duplicates: int
    error: Optional[str] = None


class BriefOut(BaseModel):
    id: int
    generated_at: dt.datetime
    tone: str
    content: str
    delivered_at: Optional[dt.datetime] = None
    meta: Optional[dict] = None

    class Config:
        from_attributes = True


class NotificationStatus(BaseModel):
    """What the Drawer chip reads. Doesn't leak secrets."""
    configured: bool
    backend: Optional[str] = None
    scheme: Optional[str] = None


class HealthOut(BaseModel):
    status: str
    sources: int
    entries: int
    db: str
    redis: str
    last_fetch: Optional[dt.datetime] = None
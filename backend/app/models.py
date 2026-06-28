"""SQLAlchemy 2.0 ORM models for Popping.

Schema mirrors the data model in the project prompt. Embedding columns are
present from day one (pgvector) but populated only in phase 2+.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Source: a registered feed/API/scrape target.
# ---------------------------------------------------------------------------


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # rss / api / scrape
    category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_interval_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    last_fetch_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Multiplier on the source's contribution to composite score (phase 2).
    # Default 1.0; the UI for tuning this lands in phase 3.
    source_weight: Mapped[float] = mapped_column(Float, default=1.0)
    # Remote URL of the source's favicon (typically origin's /favicon.ico).
    # NULL until the first ingest downloads it. The local cache path is
    # in ``favicon_path`` (extension varies — .ico, .png, .svg); the
    # frontend renders <img src=/assets/{favicon_path}>.
    favicon_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    favicon_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    entries: Mapped[list["Entry"]] = relationship(back_populates="source")


# ---------------------------------------------------------------------------
# Entry: one ingested item (article, deal, CVE, video, podcast episode, ...).
# ---------------------------------------------------------------------------


class Entry(Base):
    __tablename__ = "entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    published_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    raw_score: Mapped[float] = mapped_column(Float, default=0.0)
    personal_score: Mapped[float] = mapped_column(Float, default=0.0)
    composite_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)

    body_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    body_text_compressed: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding: Mapped[Optional[list[float]]] = mapped_column(Vector(384), nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(postgresql.JSONB, nullable=True)
    # Remote URL of the entry's thumbnail (parsed from the feed by the
    # RSS plugin; NULL for sources that don't ship images). The local
    # cache lives at /app/assets/thumbnails/<id>.<ext>; the frontend
    # renders <img src=/assets/{image_path}>.
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    source: Mapped[Source] = relationship(back_populates="entries")
    interactions: Mapped[list["Interaction"]] = relationship(back_populates="entry")


# ---------------------------------------------------------------------------
# Interaction: user engagement events feeding the For You model.
# ---------------------------------------------------------------------------


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("entries.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(60), default="default", nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # click/hover/dwell/thumb_*/bookmark/share/never
    value: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    entry: Mapped[Entry] = relationship(back_populates="interactions")

    __table_args__ = (Index("ix_interactions_user_entry", "user_id", "entry_id"),)


# ---------------------------------------------------------------------------
# Watchlist: price targets, repo alerts, CVE patch tracking.
# ---------------------------------------------------------------------------


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # amazon/product/cve/repo
    target: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_checked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_notified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# UserProfile: single-row table for personalization state.
# ---------------------------------------------------------------------------


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    preference_vector: Mapped[Optional[list[float]]] = mapped_column(Vector(384), nullable=True)
    interest_clusters: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    followed_teams: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    tracked_repos: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    running_stack: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Phase 2: category-based boost/mute (multiplicative on personal_score).
    followed_categories: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    muted_categories: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    quiet_hours_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # hour 0-23
    quiet_hours_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("id", name="uq_user_profiles_single_row"),
    )


# ---------------------------------------------------------------------------
# Session: DB-backed session row, looked up on every authenticated request.
# The cookie holds only the opaque ``id``; user data lives here so a
# container restart doesn't log everyone out.
# ---------------------------------------------------------------------------


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sub: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    auth_method: Mapped[str] = mapped_column(String(20), nullable=False)  # oidc | local | bypass
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# Brief: AI-generated digest snapshots.
# ---------------------------------------------------------------------------


class Brief(Base):
    __tablename__ = "briefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    tone: Mapped[str] = mapped_column(String(20), default="terse")  # terse / narrative / alert
    content: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Free-form bag used for dedup:
    #   {"notified_urls": [...]} — CVEs / high-severity alerts already pushed
    #   {"alert_slugs": [...]}   — convergence clusters already alerted on
    # GIN-indexed so ``meta @> '{"notified_urls": [<url>]}'::jsonb`` is cheap.
    meta: Mapped[Optional[dict]] = mapped_column(postgresql.JSONB, nullable=True)
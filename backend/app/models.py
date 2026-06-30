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
    # Free-form per-source HTTP header overrides. Merged on top of
    # ``_DEFAULT_HEADERS`` (see ``app/sources/rss.py``) at fetch
    # time. Used for feeds whose CDN blocks our default
    # ``Popping/0.2`` User-Agent (CBC) — the user points it at a
    # browser-shaped UA via the FeedManager's "advanced" section
    # without changing the global UA. Validated at the route layer
    # (``routes/sources._validate_custom_headers``): ``str → str``
    # only, with a small denylist of headers we don't want anyone to
    # be able to set (Cookie, Authorization, Host).
    custom_headers: Mapped[Optional[dict]] = mapped_column(
        postgresql.JSONB, nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    entries: Mapped[list["Entry"]] = relationship(back_populates="source")

    @property
    def auto_disabled(self) -> bool:
        """True if the source is currently inactive AND its error count
        meets/exceeds the scheduler's auto-disable threshold. Used by
        the FeedManager to label an auto-disabled row distinctly from
        one the user paused manually — the former needs the user to
        investigate ``last_error`` before re-enabling; the latter is a
        routine user choice.

        Defined as a computed property (rather than a persisted column)
        because the threshold lives in ``app.scheduler``; promoting it
        to a column would duplicate that value or require a runtime
        lookup. Computing it on read keeps the schema in lock-step with
        the scheduler's threshold by construction.
        """
        # Imported lazily to avoid a circular import at module load
        # (models is imported by scheduler's plugin chain, and
        # scheduler imports models).
        from app.scheduler import _AUTO_DISABLE_THRESHOLD
        return (not self.active) and (self.error_count or 0) >= _AUTO_DISABLE_THRESHOLD


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
    # Per-entry summary extracted on first request. Populated by
    # POST /api/entries/{id}/summary from ``meta.summary`` (with
    # HTML stripped and a length cap) so the second read is just a
    # column fetch — no re-extract. NULL means "not asked yet" (vs
    # empty string which would mean "asked, none available"). The
    # column lives on Entry rather than being computed every time
    # so a future LLM-summary path can populate the same column
    # under a different source without another migration.
    cached_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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


# ---------------------------------------------------------------------------
# AppSetting: key/value store for runtime-overridable configuration.
# ---------------------------------------------------------------------------
# Lets the operator change settings (currently: LLM provider + model
# name) from the UI without restarting the container. Persisted to the
# DB so the choice survives restarts; env vars seed the table on first
# boot only — after that the table is authoritative.
#
# Free-form ``value`` (TEXT) so we can add more keys without a
# migration; the schema-level validation lives in the route handler
# that accepts PUTs.
# ---------------------------------------------------------------------------


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# NotificationDedup: dedup ledger for outbound alert paths (CVE URL,
# convergence slug). Replaces the old "store on the latest Brief row's
# meta and truncate at 500" approach, which silently dropped older
# entries and re-notified the same CVE. Composite PK (kind, key) makes
# INSERT … ON CONFLICT DO NOTHING atomic; no truncation needed because
# rows are pruned on their own clock by future maintenance.
# ---------------------------------------------------------------------------


class NotificationDedup(Base):
    __tablename__ = "notification_dedup"

    # Small discriminator. ``cve_url`` for CVE URL dedup,
    # ``convergence_slug`` for cross-source convergence alerts.
    # New kinds land here without a schema change.
    kind: Mapped[str] = mapped_column(String(40), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    last_notified_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
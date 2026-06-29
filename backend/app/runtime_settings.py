"""DB-backed runtime settings, layered on top of pydantic env.

Read precedence (per ``get``):

    1. Row in ``app_settings`` with that key (if present and non-empty).
    2. Env-backed ``settings`` field (cold start / first boot).
    3. ``default`` argument (the consumer's hardcoded fallback).

The first-boot seeder (``seed_from_env``) copies the relevant env values
into the table ONLY if the table is empty for that key. After that the
table is authoritative — an .env edit won't silently override a choice
the user made in the UI. This is the contract documented in the README
and matches the LLM router's expectation that ``provider_for`` is
stable across restarts once the user has picked one.

Caching: ``get`` caches successful reads for ``_CACHE_TTL_SECONDS``.
``set`` updates the cache with the new value so ``snapshot_sync``
(the Router's sync hot path) sees it without a DB round-trip;
``delete`` invalidates the cache for the affected key. Caching matters
because the LLM router calls ``get`` on every request that asks for a
provider, and we don't want each Brief generation to issue a SELECT.

Key namespaces: runtime keys use dotted form for grouping
(``llm.model_brief``) so the DB stays readable as more knobs are
added. Settings field names are flat (e.g. ``ollama_cloud_model_brief``)
because pydantic-settings maps them 1:1 from env vars. The
``_SETTINGS_FIELDS`` table below is the translation map. Add a row
here when adding a new settable knob.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import AppSetting

logger = logging.getLogger("popping.runtime_settings")


# ---------------------------------------------------------------------------
# Cache — module-level; the runtime-settings table is small (a handful
# of rows), so a flat dict is fine. Values are (value, expires_at).
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 5.0
_cache: dict[str, tuple[str, float]] = {}


def _cache_get(key: str) -> Optional[str]:
    hit = _cache.get(key)
    if hit is None:
        return None
    value, expires_at = hit
    if expires_at < time.monotonic():
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: str) -> None:
    _cache[key] = (value, time.monotonic() + _CACHE_TTL_SECONDS)


def _cache_invalidate(key: str) -> None:
    _cache.pop(key, None)


# ---------------------------------------------------------------------------
# Translation: runtime key → Settings field name
# ---------------------------------------------------------------------------

# Maps a runtime_settings key (dotted, namespaced) to the Settings
# attribute (flat, env-mapped). Used both by ``get`` (env fallback) and
# ``seed_from_env`` (first-boot seeding).
#
# Adding a knob: add a Settings field on app.config.Settings, then
# add a row here. The setter / getter don't need any new code.
_SETTINGS_FIELDS: dict[str, str] = {
    "llm.provider": "",  # no env default — user picks via the chip
    "llm.model_brief": "ollama_cloud_model_brief",
    "llm.model_scoring": "ollama_cloud_model_scoring",
    "brief.window_hours": "brief_window_hours",
}


# ---------------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------------


async def _db_get(key: str) -> Optional[str]:
    async with SessionLocal() as session:
        stmt = select(AppSetting.value).where(AppSetting.key == key)
        row = (await session.execute(stmt)).first()
        return row[0] if row else None


async def _db_get_many(keys: list[str]) -> dict[str, str]:
    """Bulk load values for ``keys`` in a single query. Only returns
    rows for keys that exist AND have non-empty values — empty-string
    is treated as "no value" to match ``_db_get`` semantics. Used by
    the GET /api/settings endpoint to avoid N round-trips on the
    picker hot path."""
    if not keys:
        return {}
    async with SessionLocal() as session:
        stmt = select(AppSetting.key, AppSetting.value).where(
            AppSetting.key.in_(keys), AppSetting.value != ""
        )
        return {key: value for key, value in (await session.execute(stmt)).all()}


async def _db_set(key: str, value: str) -> None:
    """Upsert a row. SQLAlchemy's ``merge`` keeps this single-statement
    (INSERT … ON CONFLICT) and avoids the read-modify-write dance.

    The ``value`` column is VARCHAR, but the env-backed values we seed
    on first boot can be int (e.g. ``brief_window_hours``) or bool
    depending on the Settings field. We coerce to str here so any
    stringifiable value lands cleanly without the caller having to
    pre-stringify — keeps the public ``set`` contract simple.
    """
    if not isinstance(value, str):
        value = str(value)
    async with SessionLocal() as session:
        async with session.begin():
            row = AppSetting(key=key, value=value)
            await session.merge(row)


async def _db_delete(key: str) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            stmt = select(AppSetting).where(AppSetting.key == key)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is not None:
                await session.delete(row)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get(key: str, *, default: Any = None) -> Any:
    """Read a runtime setting. Returns the DB value if set and non-empty,
    else the env value (Settings field per ``_SETTINGS_FIELDS``), else
    ``default``.

    Cache hit means we never touch the DB on a warm read. The cache is
    per-process; in a multi-worker deploy each worker rebuilds its cache
    on first read, which is fine because the table is tiny.
    """
    cached = _cache_get(key)
    if cached is not None:
        return cached

    db_value = await _db_get(key)
    if db_value:
        _cache_set(key, db_value)
        return db_value

    # Fall through to env. Look up the Settings attribute via the
    # translation table; unknown keys (e.g. llm.provider, which has no
    # env equivalent) return default.
    field = _SETTINGS_FIELDS.get(key, "")
    if field:
        env_value = getattr(settings, field, None)
        if env_value:
            return env_value

    return default


async def get_many(keys: list[str]) -> dict[str, Optional[str]]:
    """Bulk-read wrapper around ``get``. One DB round-trip for all
    misses; cache hits served without I/O. Empty / unknown keys
    return ``None`` rather than ``default`` so callers can tell
    "key absent" from "key absent and no env fallback".
    """
    out: dict[str, Optional[str]] = {}
    missing: list[str] = []
    for key in keys:
        cached = _cache_get(key)
        if cached is not None:
            out[key] = cached
        else:
            out[key] = None
            missing.append(key)
    # Bulk fetch only the keys we actually miss in cache. One query
    # covers all of them. ``_db_get_many`` already filters empties.
    if missing:
        rows = await _db_get_many(missing)
        for key, value in rows.items():
            _cache_set(key, value)
            out[key] = value
    # Anything still None gets the env fallback (single getattr per key).
    for key in missing:
        if out[key] is not None:
            continue
        field = _SETTINGS_FIELDS.get(key, "")
        if field:
            env_value = getattr(settings, field, None) or None
            out[key] = env_value
    return out


async def set(key: str, value: str) -> None:
    """Write a runtime setting.

    Updates the in-process cache to the new value AND invalidates
    so any future ``get`` hits the DB. The cache update matters because
    ``snapshot_sync`` (the Router's sync hot path) reads from the
    cache — if we only invalidated, the next snapshot would fall back
    to env and silently serve stale data on a cold cache.
    """
    _cache_set(key, value)
    await _db_set(key, value)


async def delete(key: str) -> None:
    """Remove a runtime setting.

    After this call ``get`` falls back to the env value, and
    ``snapshot_sync`` excludes the key. Cache is invalidated (not just
    set to None) so the DB read on the next ``get`` happens fresh.
    """
    _cache_invalidate(key)
    await _db_delete(key)


# ---------------------------------------------------------------------------
# First-boot seeding
# ---------------------------------------------------------------------------


async def seed_from_env() -> None:
    """Copy env values into ``app_settings`` for any key the user hasn't
    explicitly set yet.

    Idempotent — re-running on a populated table is a no-op (existing
    rows are NOT overwritten). This is what lets the user's UI choice
    win over an .env edit: the seeder only fills blanks.

    Called from the app lifespan after the DB is reachable. If the DB
    isn't up yet, fail loudly — the scheduler is going to hit the same
    wall on its first job and the operator needs to see that on startup.
    """
    async with SessionLocal() as session:
        existing = {
            row[0]
            for row in (await session.execute(select(AppSetting.key))).all()
        }

    inserted = 0
    for runtime_key, settings_field in _SETTINGS_FIELDS.items():
        if not settings_field:
            # No env equivalent (e.g. ``llm.provider`` — user picks via
            # the chip). Skip.
            continue
        if runtime_key in existing:
            continue
        env_value = getattr(settings, settings_field, None)
        if not env_value:
            continue
        # Use ``set`` rather than ``_db_set`` so the in-process cache
        # is populated. Without this, snapshot_sync (the Router's sync
        # hot path) would fall through to env on the first read after
        # startup — fine for first boot, but on a restart after the
        # user has saved a value in the DB, env doesn't have it and
        # the wrong model gets picked.
        await set(runtime_key, env_value)
        inserted += 1
        logger.info(
            "runtime_settings: seeded %s=%s from env (field=%s)",
            runtime_key, env_value, settings_field,
        )

    if inserted:
        logger.info("runtime_settings: seeded %d setting(s) from env", inserted)


async def warm_cache() -> None:
    """Populate the in-process cache from existing DB rows.

    Called once during the app lifespan, after ``seed_from_env``.
    On restart, ``seed_from_env`` is a no-op (rows exist), so without
    this, ``snapshot_sync`` would fall through to env on a cold
    cache and serve stale values until something async calls
    ``get``. With this, the cache is hot from t=0 and the Router's
    first request after restart uses the user's saved choices.

    Errors are swallowed — a missing table at startup is a known
    transient (the alembic migration runs before the container is
    healthy). The first ``get`` after the table appears will
    repopulate the cache on demand.
    """
    try:
        async with SessionLocal() as session:
            rows = (await session.execute(select(AppSetting.key, AppSetting.value))).all()
        for key, value in rows:
            if value:
                _cache_set(key, value)
        if rows:
            logger.info("runtime_settings: warmed cache with %d row(s)", len(rows))
    except Exception as exc:
        logger.warning("runtime_settings: warm_cache failed (%s) — cache stays cold", exc)


def invalidate_all() -> None:
    """Wipe the in-process cache. Used by tests; safe to call from
    anywhere — the next ``get`` repopulates it from the DB."""
    _cache.clear()


def snapshot_sync() -> dict[str, str]:
    """Return the current in-process view of the runtime settings.

    This is the sync counterpart to ``get`` — used by the LLM Router on
    its hot path (``provider_for`` runs once per Brief generation).
    Keys not yet cached are filled in from env (via the translation
    table) so the Router has a complete view without a DB round-trip.

    No DB I/O. ``set`` writes through the cache, so a value saved in
    the same process is immediately visible to ``snapshot_sync``.
    ``warm_cache`` is called once at lifespan start so on restart the
    cache is hot from t=0 (no env fallback needed).
    """
    out: dict[str, str] = {}
    for runtime_key, settings_field in _SETTINGS_FIELDS.items():
        cached = _cache_get(runtime_key)
        if cached is not None:
            out[runtime_key] = cached
            continue
        if not settings_field:
            continue
        env_value = getattr(settings, settings_field, None)
        if env_value:
            out[runtime_key] = env_value
    return out
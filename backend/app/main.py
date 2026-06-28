"""FastAPI application entrypoint.

Lifespan:
  - Up: configure logging, load embedder (lazy + async so startup
    isn't blocked on model import), start scheduler (which also runs
    one immediate fetch per plugin). Embedding backfill is scheduled
    by the scheduler itself.
  - Down: stop scheduler, dispose engine.

Alembic is run by the Dockerfile's CMD (`alembic upgrade head && uvicorn`),
so by the time the app starts, the schema is already current.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import assets
from app.config import settings
from app.embeddings import embedder
from app.notify import build_notifier
from app.request_state import set_notifier
from app import runtime_settings
from app.routes import brief as brief_routes
from app.routes import entries as entries_routes
from app.routes import foryou as foryou_routes
from app.routes import health as health_routes
from app.routes import ingest as ingest_routes
from app.routes import settings as settings_routes
from app.routes import sources as sources_routes
from app.scheduler import start_scheduler, stop_scheduler


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("popping")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("popping starting")
    # Create the asset cache dirs so the /assets mount never 404s on a
    # fresh volume. Idempotent. If the assets dir isn't writable (e.g.
    # the named volume wasn't mounted when running outside compose),
    # log and keep going — the StaticFiles mount will serve an empty
    # dir and favicons/thumbnails will silently stay missing.
    try:
        assets.ensure_dirs()
    except OSError as exc:
        logger.warning(
            "assets: cannot create %s (%s) — favicons/thumbnails will be unavailable",
            settings.assets_dir, exc,
        )
    # Load embedder first — the scheduler's ingest path will call
    # embed() on every entry, and we want the model warm before the
    # first fetch lands. If the model download fails (offline cold
    # start, HuggingFace unreachable) or embeddings are explicitly
    # disabled, this is a no-op. Never crash startup on it — ingest
    # degrades to recency + source weight when embeddings are absent
    # (see app.config: embedding_enabled docstring).
    try:
        await embedder().load()
    except Exception:
        logger.exception(
            "embeddings: failed to load model '%s' — continuing without embeddings",
            settings.embedding_model,
        )
    # Build the notifier once. Both scheduler jobs and the brief
    # endpoint read it from app.request_state. ``None`` means "no
    # backend configured" — everything keeps working without pushes.
    notifier = build_notifier()
    set_notifier(notifier)
    if notifier is not None:
        logger.info("notifications: configured (%s)", notifier.name)
    else:
        logger.info("notifications: no backend configured")
    # Seed the runtime_settings table from env on first boot only —
    # ``seed_from_env`` is a no-op when the table already has rows, so
    # subsequent restarts don't clobber the user's UI choices. Wrapped
    # so a DB hiccup doesn't block the rest of startup; the picker
    # then falls through to env values, which is the safe default.
    try:
        await runtime_settings.seed_from_env()
    except Exception:
        logger.exception("runtime_settings: seed failed — falling back to env")
    # Warm the in-process cache from existing DB rows so the Router
    # serves saved choices on the very first request after restart.
    # Idempotent with seed_from_env (no-op if the table is empty).
    await runtime_settings.warm_cache()
    await start_scheduler(notifier=notifier)
    try:
        yield
    finally:
        await stop_scheduler()
        logger.info("popping stopped")


app = FastAPI(
    title="Popping",
    version="0.1.0",
    description="Personal AI-ranked intelligence dashboard",
    lifespan=lifespan,
)

# CORS — single-origin in production (frontend served from the same host
# via Vite's dev proxy). The dev proxy makes /api/* same-origin so cookies
# flow naturally; with `credentials` set on the fetch wrapper, the
# session cookie rides on every API call.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_routes.router, prefix="/api")
app.include_router(sources_routes.router, prefix="/api")
app.include_router(entries_routes.router, prefix="/api")
app.include_router(foryou_routes.router, prefix="/api")
app.include_router(ingest_routes.router, prefix="/api")
app.include_router(brief_routes.router, prefix="/api")
app.include_router(settings_routes.router, prefix="/api")

# Auth router is only mounted when OIDC is enabled — keeps single-user
# deployments free of /auth/* routes entirely.
if settings.oidc_enabled:
    from app.auth.routes import router as auth_router

    app.include_router(auth_router)
    logger.info("OIDC auth enabled (issuer=%s)", settings.oidc_issuer)

# Cached asset files (favicons + thumbnails). Mounted last so the API
# routers above always win for /api/* paths. The browser loads these
# as same-origin <img> tags — no third-party referer leak, no CORS.
app.mount("/assets", StaticFiles(directory=settings.assets_dir), name="assets")
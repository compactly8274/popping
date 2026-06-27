"""FastAPI application entrypoint.

Lifespan:
  - Up: configure logging, start scheduler (which also runs one immediate
    fetch per plugin), nothing else needed.
  - Down: stop scheduler, dispose engine.

Alembic is run by the Dockerfile's CMD (`alembic upgrade head && uvicorn`),
so by the time the app starts, the schema is already current.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routes import entries as entries_routes
from app.routes import health as health_routes
from app.routes import ingest as ingest_routes
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
    await start_scheduler()
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
app.include_router(ingest_routes.router, prefix="/api")

# Auth router is only mounted when OIDC is enabled — keeps single-user
# deployments free of /auth/* routes entirely.
if settings.oidc_enabled:
    from app.auth.routes import router as auth_router

    app.include_router(auth_router)
    logger.info("OIDC auth enabled (issuer=%s)", settings.oidc_issuer)
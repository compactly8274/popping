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
    logger.info("popping starting (backend on %s:%d)", settings.backend_host, settings.backend_port)
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

# CORS — phase 1 is single-origin, but the dev port may differ from the
# production one. Loosening CORS here is fine because the backend binds
# to 127.0.0.1 by default and isn't reachable from the public internet.
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
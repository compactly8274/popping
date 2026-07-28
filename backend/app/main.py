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
from app.embeddings import close_embedder, embedder
from app.notify import build_notifier
from app import reddit_client
from app.request_id import bind_request_id, clear_request_id, current_request_id
from app.request_state import set_notifier
from app import runtime_settings
from app.routes import brief as brief_routes
from app.routes import entries as entries_routes
from app.routes import foryou as foryou_routes
from app.routes import framing as framing_routes
from app.routes import health as health_routes
from app.routes import ingest as ingest_routes
from app.routes import interactions as interactions_routes
from app.routes import preferences as preferences_routes
from app.routes import settings as settings_routes
from app.routes import sources as sources_routes
from app.redis import close_redis, init_redis
from app.db import dispose_engine
from app.scheduler import start_scheduler, stop_scheduler


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s[%(request_id)s]: %(message)s",
)


class _RequestIdFilter(logging.Filter):
    """Inject the current request id into every log record.

    Pulls from the ``ContextVar`` set by the request middleware
    (see ``app.request_id.bind_request_id``). When no request is
    in flight — startup, shutdown, scheduler ticks, background
    tasks launched outside any request — the value is ``None``
    and the formatter renders it as ``-`` so the segment is
    present-but-empty rather than missing, which keeps log
    parsing tools happy.

    A ``LogRecord`` gains a ``request_id`` attribute (str) —
    FastAPI/starlette's logging chain doesn't touch this name,
    so adding it is safe.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        return True


# Attached to the root logger's HANDLER, not the root Logger object.
# A filter added via ``Logger.addFilter()`` only runs for records
# that originate on that exact logger (``Logger.handle()`` calls
# ``self.filter(record)`` on itself, once, before ``callHandlers``
# walks the chain) — it does NOT run for records from child loggers
# like ``logging.getLogger("popping.scheduler")`` propagating up,
# which is how virtually every log call in this app is made. Adding
# the filter to root would leave ``record.request_id`` unset for
# those, and the format string's ``%(request_id)s`` would then raise
# ``KeyError`` inside the formatter on every single one of them —
# reproduced directly: every "popping.*" log call failed with
# "Logging error" / "Formatting field not found in record:
# 'request_id'", drowning out real log output (including the actual
# tracebacks needed to debug requests) without crashing the process.
# A filter added to the HANDLER runs for every record that reaches
# it regardless of which logger originated it, which is what we want
# here.
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RequestIdFilter())
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
    # Build the shared HTTP client used by the favicon + thumbnail
    # fetchers. Connection pooling across fetches amortises the TCP/
    # TLS handshake — a 50-item ingest would otherwise pay 150+
    # handshakes. Set on startup; closed on teardown so connection
    # pools don't leak across ``uvicorn --reload`` cycles.
    assets.init_client()
    # Build the Hydra Reddit client. No-op when REDDIT_HYDRA_URL is
    # unset (feature off); otherwise builds a shared client with the
    # bearer token from settings. Kept separate from the assets client
    # because (a) the bearer token shouldn't leak to the thumbnail
    # fetcher, (b) per-call timeouts diverge (Hydra wants short).
    reddit_client.init_client()
    # Build the Redis pool up-front so the first request doesn't pay
    # the connect round-trip. ``init_redis`` is a no-op if the URL
    # is unset (pure file-system deploy), so we don't gate it on
    # ``settings.redis_url``. A failed Redis call will be raised
    # here and logged; if you want graceful degrade, wrap in try/
    # except — today we let it crash because the rest of the app
    # assumes Redis exists.
    await init_redis()
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
        # Teardown order: scheduler first (so no in-flight fetches
        # race the closes), then the shared asset client, the Hydra
        # Reddit client (same idempotent shape), the Redis pool, the
        # embedder's ThreadPoolExecutor (otherwise the worker
        # threads leak across ``uvicorn --reload`` cycles), and
        # finally the SQLAlchemy engine. Each close is idempotent —
        # a missing/broken subsystem just no-ops.
        await stop_scheduler()
        await assets.close_client()
        await reddit_client.close_client()
        await close_redis()
        await close_embedder()
        await dispose_engine()
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
#
# The allowlist is built from ``settings.public_url`` (the production
# origin the dashboard is served from) plus ``settings.extra_cors_origins``
# (a comma-separated list of additional origins — e.g. a LAN IP
# when accessing the dashboard from a phone, a staging alias).
# ``allow_credentials=False`` keeps this safe even with a misconfigured
# allowlist — the browser will refuse to send the session cookie
# cross-origin, so the worst case is "JS on origin A can read
# public data on the API" (which is what ``public_url`` says is OK).
#
# If BOTH settings are empty (the default docker-compose dev
# setup where ``PUBLIC_URL`` is unset), we fall back to ``["*"]`` for
# backward compatibility — dev-mode access from any origin still
# works. In production the operator MUST set PUBLIC_URL (or
# ``public_url`` + ``extra_cors_origins`` if the frontend is on a
# different host).
_allow_origin_set = set()
if settings.public_url:
    # ``public_url`` is the canonical "where the dashboard is served
    # from" — strip any trailing slash so ``https://x.example.com``
    # and ``https://x.example.com/`` don't end up as two distinct
    # allowlist entries (CORS matches the request's ``Origin``
    # header, which never has a trailing slash for a bare host).
    _allow_origin_set.add(settings.public_url.rstrip("/"))
if settings.extra_cors_origins:
    # Comma-separated list. Each entry is a full origin
    # (``http://192.168.1.10:8080``); trim whitespace and drop
    # empties so a trailing comma in the env doesn't leave an
    # empty string in the allowlist.
    for entry in settings.extra_cors_origins.split(","):
        stripped = entry.strip().rstrip("/")
        if stripped:
            _allow_origin_set.add(stripped)
_cors_origins = (
    sorted(_allow_origin_set) if _allow_origin_set else ["*"]
)
logger.info(
    "cors: %d origin(s) allowed (public_url=%r, extra=%r)",
    len(_cors_origins),
    settings.public_url,
    settings.extra_cors_origins,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_routes.router, prefix="/api")
app.include_router(sources_routes.router, prefix="/api")
app.include_router(entries_routes.router, prefix="/api")
app.include_router(foryou_routes.router, prefix="/api")
app.include_router(framing_routes.router, prefix="/api")
app.include_router(ingest_routes.router, prefix="/api")
app.include_router(interactions_routes.router, prefix="/api")
app.include_router(preferences_routes.router, prefix="/api")
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
# ``X-Content-Type-Options: nosniff`` is set on /assets/* responses by
# the middleware below so the browser can't reinterpret an
# attacker-served file (e.g. an SVG that contains a script) as a
# different MIME type. Combined with the allowlist-only content-type
# check in assets._download, this is the second line of defence
# against the "HTML-as-favicon" stored-XSS class of bug.
app.mount("/assets", StaticFiles(directory=settings.assets_dir), name="assets")


@app.middleware("http")
async def _assets_security_headers(request, call_next):
    """Add ``X-Content-Type-Options: nosniff`` to /assets/* responses.

    StaticFiles sets its own content-type from the file extension
    (which is derived from a strict image allowlist in
    ``assets._download``), but ``nosniff`` is a belt-and-suspenders
    against any future content-type path that might serve a
    same-origin file the browser could reinterpret. Doesn't touch
    the API surface (those responses are JSON; nosniff is harmless
    there)."""
    response = await call_next(request)
    if request.url.path.startswith("/assets/"):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


# Slice 28 (security): defense-in-depth headers on every response.
# The clickjacking concern on /auth/login + /auth/callback is real
# (those are the only redirect-shaped endpoints that take user
# input via URL params). CSP ``frame-ancestors 'none'`` is the
# modern way to forbid framing; ``X-Frame-Options DENY`` is the
# legacy header for browsers that don't honor CSP frame-ancestors.
# HSTS only fires when the public_url is https — sending it over
# http would be a no-op for the user (browsers ignore HSTS over
# http) but a fingerprintable header.
#
# ``Referrer-Policy: no-referrer`` — the dashboard never needs to
# send a referer to third-party resources; same-origin already
# knows what page you're on. Reduces accidental referer leakage
# from any ``window.open`` we might add later.
@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    # ``frame-ancestors 'none'`` is the modern CSP directive; X-Frame-Options
    # is the legacy fallback. Browsers ignore the legacy one if the modern
    # one is present, but old browsers (and some non-browser embedders)
    # still need it.
    response.headers.setdefault(
        "Content-Security-Policy", "frame-ancestors 'none'"
    )
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if settings.public_url and settings.public_url.startswith("https://"):
        # 1-year HSTS — long enough to be a real commitment, short
        # enough that a config mistake can recover in a year.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000"
        )
    return response


@app.middleware("http")
async def _request_id(request, call_next):
    """Bind a request id for the duration of the request, surface
    it on the response, and (via ``_RequestIdFilter`` above) on
    every log line emitted while the request is in flight.

    Honors ``X-Request-Id`` from the client if present (so a
    browser-side correlate can pre-generate the id and trace a
    single user action from click to log line). Otherwise
    generates a 12-char hex token. The bound value is also
    set on the response as ``X-Request-Id`` so the client can
    read it back — the operator (who is also the user in this
    single-user dashboard) can copy/paste that id when
    reporting an issue.

    Static-file requests (under ``/assets/``) get the same
    treatment: they can fail (asset dir missing, file
    disappeared) and the operator needs the same trace
    correlation the API surface gets.

    The contextvar is reset on the way out so a follow-up
    request on the same asyncio task (rare under uvicorn,
    possible under some test fixtures) doesn't inherit the
    previous request's id.
    """
    incoming = request.headers.get("X-Request-Id")
    rid = bind_request_id(incoming)
    try:
        response = await call_next(request)
    finally:
        # Reset on the way out so a leaked task (e.g. one
        # scheduled by a BackgroundTasks dependency) starts
        # with a clean slate rather than inheriting a
        # request-id that no longer matches any in-flight
        # request. NOTE: this lives in a ``finally`` so the
        # reset happens even when ``call_next`` raises; the
        # exception still propagates to FastAPI's normal
        # 500 handler.
        clear_request_id()
    response.headers["X-Request-Id"] = rid
    return response
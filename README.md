# Popping

Personal AI-ranked intelligence dashboard. Aggregates deals, vulnerabilities, news, sports, podcasts, YouTube, and more into a single scored feed.

**Phase 1 scope**: scaffold + 1 RSS source (BBC News) + minimal UI. The schema and source-plugin interface are designed to absorb the rest of the categories (deals, vulns, sports, etc.) in later phases without reshaping the database.

## Quickstart

**Prebuilt images (production, no build step):**

```bash
cp .env.example .env             # defaults work out of the box
docker compose up -d             # pulls ghcr.io/compactly8274/popping-{backend,frontend}:latest
open http://<server-ip>:14789    # frontend (the only published port)
```

The frontend publishes on `14789` by default — a 5-digit port in the
14xxx range chosen to stay out of the common 3000/5173/8000/8080/8443
range and below typical ephemeral-port territory (32768+). Override with
`POPPING_FRONTEND_PORT=<port>` in `.env` (the container still listens on
5173; only the host-side mapping changes). The backend is reachable from
the frontend over the internal docker network (`http://backend:8000`);
postgres and redis are entirely internal.

**Headless / LAN deployments** — the frontend binds to `0.0.0.0:14789` by
default, so any device on the network can reach it. Put a reverse proxy
in front (Caddy / Traefik / Nginx) for TLS + (optionally) additional
auth in front of OIDC. Enable OIDC (see below) before exposing on a LAN.

The first boot runs `alembic upgrade head` against a fresh postgres volume, so the schema is created automatically. The scheduler then fires one immediate fetch per plugin and re-fetches every `refresh_interval_seconds`.

**Local dev with hot-reload (build from source):**

```bash
cp .env.example .env
cp docker-compose.override.yml.example docker-compose.override.yml
docker compose up -d --build     # builds from ./backend and ./frontend, bind-mounts source
```

The override is auto-loaded by `docker compose up` and only adds `build:` + bind mounts; postgres and redis stay pulled.

**Pin to a specific build:**

```bash
# In .env:
POPPING_IMAGE_TAG=sha-50af121    # exact commit
# or
POPPING_IMAGE_TAG=pr-3           # test a PR
POPPING_PULL_POLICY=always       # force a fresh pull
```

## Architecture

```
┌──────────────┐   ┌──────────────┐
│  Vite + R+T  │──▶│  FastAPI     │──┐
│  (frontend)  │   │  (backend)   │  │
└──────────────┘   └──────┬───────┘  │
                          │          │
                   ┌──────▼──────┐   │
                   │ APScheduler │   │
                   │  (sources)  │   │
                   └──────┬──────┘   │
                          │          │
              ┌───────────┼──────────┤
              ▼           ▼          ▼
        ┌─────────┐  ┌────────┐  ┌─────────┐
        │Postgres │  │ Redis  │  │ Source  │
        │pgvector │  │        │  │ plugins │
        └─────────┘  └────────┘  └─────────┘
```

- **Backend** (`backend/`): FastAPI + SQLAlchemy 2 async + APScheduler + pgvector.
- **Frontend** (`frontend/`): Vite + React + Tailwind.
- **Source plugins** (`backend/app/sources/`): drop a file, restart the backend, it's registered automatically via `@register_source`.
- **Storage**: Postgres 16 with pgvector (embeddings phase 2+), Redis for caching (phase 1 unused).

## API

| Endpoint                          | Purpose                                | Auth      |
| --------------------------------- | -------------------------------------- | --------- |
| `GET  /api/health`                | Liveness + counts                      | public    |
| `GET  /api/sources`               | Registered sources + last-fetch state  | public    |
| `GET  /api/sources/{id}`          | One source                             | public    |
| `GET  /api/entries`               | List entries (`?category=&source=&limit=`) | public |
| `POST /api/ingest/{source_name}`  | Force a fetch now (instead of waiting) | login     |
| `GET  /auth/login`                | Kick off the OIDC flow (302 to IdP)    | n/a       |
| `GET  /auth/callback`             | OIDC redirect URI                      | n/a       |
| `POST /auth/logout`               | Clear the session cookie               | n/a       |
| `GET  /auth/me`                   | Current user payload or 401            | n/a       |

Interactive docs at `http://<server-ip>:14789/api/docs`.

## OIDC / login

`OIDC_ENABLED=false` (the default) keeps the dashboard as a single-user app
with no login screen — fine for a personal loopback deployment. Set
`OIDC_ENABLED=true` for any LAN or public exposure, then configure the
remaining vars in `.env`:

```bash
OIDC_ENABLED=true
OIDC_ISSUER=https://auth.example.com       # your IdP's issuer URL
OIDC_CLIENT_ID=popping                    # public client (no secret)
OIDC_SCOPES=openid email profile
PUBLIC_URL=https://popping.example.com     # must match the reverse-proxy host
SESSION_SECRET=<openssl rand -hex 32>      # required when OIDC is on
```

The backend fetches the IdP's `/.well-known/openid-configuration` on the
first login, so the IdP doesn't need to be up at container start — it only
needs to be reachable when someone clicks **Sign in**.

Flow: browser → `GET /auth/login` (302 to IdP) → consent → `GET /auth/callback?code=...`
(exchange, set session cookie, 302 to `/`). Sessions are stateless signed
cookies (8 h, `HttpOnly`, `SameSite=Lax`, `Secure` when `PUBLIC_URL` is https).

**What's gated.** Reads (`/api/entries`, `/api/sources`, `/api/health`) stay
public so embed/preview use cases work without login. Mutations
(`POST /api/ingest/{name}` today, interactions and watchlist in phase 2)
require a session.

### Authentik

1. *Applications* → *Create* → *OAuth2/OpenID*: name `popping`, provider
   *OAuth2*, client type *Public*, redirect URI
   `https://popping.example.com/auth/callback`.
2. *Providers* → *Create* → *OAuth2/OIDC*: authorization flow *Authorization
   code*, scopes *openid email profile*, signing key *Self-signed*.
3. Copy the client ID → `OIDC_CLIENT_ID`. Issuer URL → `OIDC_ISSUER`.

### Pocket-ID

1. *OIDC Clients* → *New*: name `popping`, redirect URI
   `https://popping.example.com/auth/callback`, scopes
   *openid email profile*, no client secret (PKCE only).
2. Copy the client ID → `OIDC_CLIENT_ID`. Issuer URL → `OIDC_ISSUER`.

### Google

`OIDC_ISSUER=https://accounts.google.com`, create an OAuth client in
Google Cloud Console with redirect URI `https://popping.example.com/auth/callback`.

## Adding a new source plugin

1. Create `backend/app/sources/<name>.py`:

   ```python
   from app.sources import register_source
   from app.sources.base import SourcePlugin

   @register_source
   class MySource(SourcePlugin):
       name = "my_source"             # unique
       type = "rss"                   # rss | api | scrape
       category = "news"              # news | deals | vulns | ...
       url = "https://example.com/feed.xml"
       refresh_interval_seconds = 3600

       async def fetch(self) -> list[dict]:
           # Return [{title, url, published_at, ...extras}, ...]
           ...
   ```

2. Add the import to the bottom of `backend/app/sources/__init__.py`:

   ```python
   from app.sources import rss, my_source  # noqa: F401, E402
   ```

3. Restart the backend container. The scheduler picks up the new plugin on next boot; the sources row is auto-upserted on first fetch.

## Layout

```
.
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/0001_initial.py
│   └── app/
│       ├── main.py            # FastAPI app + lifespan
│       ├── config.py          # pydantic-settings
│       ├── db.py              # async SQLAlchemy engine
│       ├── redis.py           # async Redis client
│       ├── deps.py
│       ├── auth/              # OIDC (only mounted when OIDC_ENABLED=true)
│       │   ├── settings.py    #   OIDCConfig loader
│       │   ├── session.py     #   signed-cookie sessions
│       │   ├── oidc.py        #   authlib PKCE flow
│       │   ├── deps.py        #   current_user / require_user
│       │   └── routes.py      #   /auth/login /auth/callback /auth/logout /auth/me
│       ├── models.py          # SQLAlchemy ORM (sources/entries/interactions/...)
│       ├── schemas.py         # Pydantic response shapes
│       ├── scheduler.py       # APScheduler + ingest pipeline
│       ├── scoring/recency.py # phase 1 placeholder scorer
│       ├── sources/
│       │   ├── base.py        # SourcePlugin ABC
│       │   ├── rss.py         # BBC News (the only built-in)
│       │   └── __init__.py    # @register_source + plugin discovery
│       └── routes/
│           ├── health.py
│           ├── sources.py
│           ├── entries.py
│           └── ingest.py
└── frontend/
    ├── Dockerfile
    ├── package.json
    ├── vite.config.ts
    ├── tailwind.config.js
    └── src/
        ├── App.tsx            # desktop grid + mobile swipe
        ├── api.ts
        └── components/
            ├── AuthChip.tsx   # login / sign-out chip in the header
            ├── Card.tsx
            ├── Column.tsx
            ├── Drawer.tsx
            └── Hamburger.tsx
```

## Phase roadmap

- **Phase 1** (this commit): scaffold, schema, one RSS source, minimal UI. ✅
- **Phase 2**: scoring engine (recency + source weight + personal vector), LLM provider abstraction (Ollama/Groq/Claude/OpenAI), embedding pipeline, For You feed.
- **Phase 3**: more source plugins — RedFlagDeals, NVD, CISA KEV, ESPN, YouTube RSS, Podcast Index, Keepa, GitHub releases, Wikipedia On This Day, HN, Mastodon.
- **Phase 4**: notifications (Pushover/Apprise), The Brief generator, settings drawer, dead-feed detection, dedup, converging-story detection.

## Environment variables

All optional except the database credentials (defaults work). See `.env.example` for the full list. The frontend's `VITE_BACKEND_URL` (in `docker-compose.yml`) points Vite's dev-server proxy at the backend container.

## Development notes

- **Hot reload**: both backend (`uvicorn --reload`) and frontend (Vite HMR) reload on file changes via the bind mounts in `docker-compose.yml`.
- **Schema changes**: edit `app/models.py`, then `docker compose exec backend alembic revision --autogenerate -m "..."`. Commit the new file in `alembic/versions/`.
- **Reset**: `docker compose down -v` wipes the postgres volume. Next `up` re-runs migrations from scratch.

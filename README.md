# Popping

Personal AI-ranked intelligence dashboard. Aggregates deals, vulnerabilities, news, sports, podcasts, YouTube, and more into a single scored feed.

**Phase 1 scope**: scaffold + 1 RSS source (BBC News) + minimal UI. The schema and source-plugin interface are designed to absorb the rest of the categories (deals, vulns, sports, etc.) in later phases without reshaping the database.

## Quickstart

**Prebuilt images (production, no build step):**

```bash
cp .env.example .env             # defaults work out of the box
docker compose up -d             # pulls ghcr.io/compactly8274/popping-{backend,frontend}:latest
open http://127.0.0.1:5173       # frontend
curl http://127.0.0.1:8000/api/health   # backend
```

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

| Endpoint                          | Purpose                                |
| --------------------------------- | -------------------------------------- |
| `GET  /api/health`                | Liveness + counts                      |
| `GET  /api/sources`               | Registered sources + last-fetch state  |
| `GET  /api/sources/{id}`          | One source                             |
| `GET  /api/entries`               | List entries (`?category=&source=&limit=`) |
| `POST /api/ingest/{source_name}`  | Force a fetch now (instead of waiting) |

Interactive docs at `http://127.0.0.1:8000/docs`.

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

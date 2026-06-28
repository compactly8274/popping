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
| `GET  /api/foryou`                | Personal top-N feed (with convergence boost) | login |
| `POST /api/ingest/{source_name}`  | Force a fetch now (instead of waiting) | login     |
| `GET  /auth/login`                | Kick off the OIDC flow (302 to IdP)    | n/a       |
| `GET  /auth/callback`             | OIDC redirect URI                      | n/a       |
| `POST /auth/local`                | Local-user login (form on login page)  | n/a       |
| `GET  /auth/local/availability`   | `{"enabled": bool}` — does the login page show the local form? | n/a |
| `POST /auth/logout`               | Clear the session cookie + delete row  | n/a       |
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
(`/api/ingest/{name}` today, interactions and watchlist in a later phase)
require a session.

### Local fallback user

When the IdP is down (or you're testing without one), a hardcoded local
account can log in via `POST /auth/local` — the form on the login page
exposes it automatically. Configure:

```bash
LOCAL_AUTH_ENABLED=true
LOCAL_USER_NAME=alice
LOCAL_USER_PASSWORD_HASH=<bcrypt hash>
LOCAL_USER_EMAIL=alice@lan.local       # optional, display only
```

Generate the hash:
```bash
python -c "import bcrypt; print(bcrypt.hashpw(b'YOUR_PASSWORD', bcrypt.gensalt()).decode())"
```

The endpoint runs `bcrypt.checkpw` regardless of which field is wrong, so
the response time is constant (no username enumeration via timing).

### Loopback bypass

For "I'm at the server's keyboard and don't want to round-trip through
the IdP" — flip on loopback bypass:

```bash
LOCAL_AUTH_BYPASS=true
```

Any request from `127.0.0.0/8` or `::1` is treated as authenticated with
a synthetic `local-loopback` user. Honors `X-Forwarded-For` (leftmost IP)
when behind a reverse proxy, so it only fires for genuinely local traffic
as long as your proxy is trusted to strip client-supplied headers.

Off by default; only enable when you trust the network between the
reverse proxy and the backend.

### Persistent sessions

Sessions are stored in the `sessions` table, not in the cookie. The
cookie carries only an opaque random ID; the row holds user data + TTL.
This means:

- **Restart-safe**: the backend can restart without logging anyone out.
- **Server-side revocation**: `POST /auth/logout` deletes the row.
- **Sliding TTL**: every authenticated request extends the expiry.
- **Audit-friendly**: `SELECT * FROM sessions` shows who's logged in.

A periodic purge (every hour by default; `SESSION_PURGE_INTERVAL_SECONDS`)
deletes expired rows.

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

## Phase 4 — The Brief + notifications

Phase 4 lights up the LLM router that phase 2 shipped and adds push
notifications. Three new endpoints; one new dashboard card; no
breaking changes to the schema beyond a new JSON column on
`briefs.meta` (used for dedup).

### The Brief

A daily AI-generated digest of the top 25 entries from the last 24h,
rendered as a card on the dashboard above For You. One LLM call per
Brief; no streaming. Tone defaults to `terse` (one line + 3-5
highlights + 1-3 watch items); `narrative` is also available.

**Triggers** — all three converge on `BriefGenerator.generate()`:

| Trigger | How | When |
| --- | --- | --- |
| Daily scheduled | APScheduler `CronTrigger(hour=BRIEF_SCHEDULE_HOUR)` | `BRIEF_SCHEDULE_HOUR` UTC (default 08:00). Set `-1` to disable. |
| Manual | `POST /api/brief/generate?tone=terse` | Header "Brief" button or Drawer button. Login-gated when OIDC is on. |
| Convergence | `BriefGenerator.generate_alert()` from the periodic convergence-check job | When a slug appears in `CONVERGENCE_NOTIFY_THRESHOLD` (default 2) sources within `CONVERGENCE_WINDOW_HOURS`. |

The LLM is picked via `app.llm.router.provider_for("brief")` — same
Anthropic → OpenAI → Groq → Ollama order as scoring. If nothing is
configured, the manual endpoint returns 503 ("no LLM provider") and
the scheduler logs and skips. The dashboard keeps working with the
existing Brief row.

### Notifications

Two backends, picked at startup. The Drawer shows which one is wired
up:

| Backend | Env vars | Notes |
| --- | --- | --- |
| Apprise (preferred) | `APPRISE_URL` | Opaque URL — `pover://…`, `tgram://…`, `discord://…`, `mailto://…`, `ntfy://…`. One library, 100+ services. |
| Pushover (fallback) | `PUSHOVER_USER_KEY` + `PUSHOVER_APP_TOKEN` | Plain `httpx` POST to `api.pushover.net/1/messages.json`. Used when `APPRISE_URL` is unset. |

**Three notification triggers:**

1. **Brief delivery** — every successful Brief generation pushes the
   full content via the configured backend.
2. **High-CVSS CVEs** — post-ingest hook in `_ingest` checks
   `meta.cvss_score` on newly-inserted rows. Sends a single batched
   alert per ingest when at least one entry exceeds
   `CVE_NOTIFY_MIN_CVSS` (default 7.0). Deduped via
   `Brief.meta.notified_urls` (GIN-indexed JSON containment).
3. **Convergence alerts** — periodic job (every
   `CONVERGENCE_CHECK_INTERVAL_MINUTES`, default 15) scans for
   unalerted clusters, fires `generate_alert()` (one-sentence
   `tone="alert"` Brief), and pushes that.

`Notifier.send()` is best-effort — transport failures are logged but
never raise. A broken notifier can't break ingest.

### Settings / Drawer

The Drawer now shows a "Notifications" chip at the top with the
configured backend (e.g. `configured (apprise · pover)`) plus a
"Generate brief now" button. The full settings drawer (followed
categories, source weight tuning) is still a follow-up — the model
columns exist but the UI isn't built yet.

### API additions

| Endpoint | Purpose | Auth |
| --- | --- | --- |
| `GET  /api/brief/latest?tone=&limit=` | Most recent Brief(s). Latest of each tone when `tone` is omitted. | public |
| `POST /api/brief/generate?tone=terse\|narrative` | Synchronously generate a new Brief. | login when OIDC on |
| `GET  /api/notifications/status` | `{configured, backend, scheme}` — Drawer chip. | public |

## Phase 3 — sources

Six source plugins, all anonymous (no API keys required for the core
set), all bot-tolerant. Categories:

| Source                  | Category | Refresh | Notes |
| ----------------------- | -------- | ------- | ----- |
| BBC News                | `news`   | 1 h     | RSS; phase 3 fixed the User-Agent and switched to https |
| Wikipedia On This Day   | `news`   | 12 h    | REST; today's historical events + curated anniversaries |
| Hacker News top         | `tech`   | 5 min   | Firebase API; top 30 stories, score + comment count in meta |
| GitHub releases         | `tech`   | 30 min  | 5 repos (python/cpython, nodejs/node, kubernetes/*, rust-lang/rust); ETag-cached |
| NVD recent CVEs         | `vulns`  | 6 h     | NIST CVE 2.0 API; 7-day rolling window of published CVEs |
| CISA KEV                | `vulns`  | 6 h     | Known Exploited Vulnerabilities catalog; dateAdded as the published_at |

The scheduler runs an immediate fetch per source on startup, so the
dashboard populates within ~30 seconds of the first boot. Each source
ingests independently — a GitHub rate-limit or NVD maintenance window
won't block the others.

To add a repo to the GitHub source, edit `backend/app/sources/github_releases.py`
and add to `_DEFAULT_REPOS`. To add an authenticated token for higher
rate limits, set `GITHUB_TOKEN` in `.env`.

**BBC fix.** Phase 1's RSS fetcher sent `User-Agent: popping/0.1` over
`http://`. Several RSS providers (BBC since 2024, Reddit, etc.) throttle
or 403 the default `python-httpx` UA. Phase 3 ships a descriptive UA
and an explicit `Accept: application/rss+xml, application/atom+xml, …`
header, which is enough to get past those filters without any
unconventional headers.

Sources intentionally deferred to a later phase: RedFlagDeals (rate-limit
sensitive), ESPN (sport-specific endpoints), YouTube RSS (channel
resolution), Reddit / Mastodon (privacy-policy noise), Podcast Index
and Keepa (API keys + tier-specific endpoints).

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

## Phase 2 — scoring, embeddings, For You

Phase 1's `composite_score` was just recency. Phase 2 makes the feed
actually intelligent:

- **Composite scoring** blends recency, source weight, and personal
  preference (vector cosine + followed/muted categories). The blend
  weights are env-tunable (`SCORING_WEIGHT_*`).
- **Embedding pipeline** fills `entries.embedding` with 384-dim
  sentence-transformers vectors at ingest time. One-shot backfill
  on startup handles entries from before the column was wired in.
- **LLM provider abstraction** (Ollama / Anthropic / OpenAI / Groq) is
  in place but currently unused — phase 4's Brief generator will be
  the first call site.
- **For You** (`GET /api/foryou`) is a personal top-N feed with a
  convergence boost: items sharing a normalized title across multiple
  sources within 24h get a small multiplicative bump.

### Scoring math

```
composite = w_recency * recency(published_at, category)
          + w_personal * personal(entry, source, profile)
          + w_source   * (raw_score * source_weight)

personal = vector_score * category_multiplier(followed, muted)
```

`recency` is exponential decay with per-category half-life (news/vulns:
6 h, deals: 48 h, sports: 3 h, default: 12 h). `vector_score` is cosine
similarity to the user's `preference_vector`, rescaled from `[-1, 1]` to
`[0, 100]`. NULL vectors return a neutral 50 (cold-start midpoint).

### Convergence boost

Computed at query time, not at ingest, so clusters pick up the boost
the moment they form. One SQL `GROUP BY title_slug` over the last
`CONVERGENCE_WINDOW_HOURS`, then a Python pass applies the multiplier:

- 1 source → ×1.0 (no boost)
- 2 sources → ×`CONVERGENCE_BOOST_2` (default 1.10)
- 3+ sources → ×`CONVERGENCE_BOOST_3PLUS` (default 1.20)

### Embedding backfill

On startup the scheduler queues a one-shot job (and re-runs it every
5 minutes as a safety net) that embeds any entry with a NULL embedding.
Batches of `EMBEDDING_BATCH_SIZE` (default 64). Failures are logged and
skipped — the entry stays in the table with NULL embedding, and
`personal.score` treats that as a neutral 50 cosine.

### Provider selection

The LLM router picks the first configured provider in this order:
Anthropic → OpenAI → Groq → Ollama. If nothing is configured,
`provider_for(task)` returns `None` and callers log-and-skip. Phase 2
has no call sites yet, so a no-key install is fully supported.

### Memory note

`sentence-transformers` pulls in torch (~200 MB CPU-only, ~600 MB
CUDA). The Dockerfile installs CPU-only torch explicitly to keep the
image slim. Set `EMBEDDING_ENABLED=false` to skip the whole pipeline
on a memory-constrained host; the rest of the scoring still works,
just without the vector signal.

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
│   │   └── versions/
│   │       ├── 0001_initial.py
│   │       └── 0002_auth.py   # sessions table
│   └── app/
│       ├── main.py            # FastAPI app + lifespan
│       ├── config.py          # pydantic-settings
│       ├── db.py              # async SQLAlchemy engine
│       ├── redis.py           # async Redis client
│       ├── deps.py
│       ├── auth/              # OIDC + local auth (mounted when OIDC_ENABLED=true)
│       │   ├── settings.py    #   OIDCConfig loader
│       │   ├── session.py     #   DB-backed sessions
│       │   ├── oidc.py        #   authlib PKCE flow
│       │   ├── deps.py        #   current_user / require_user (incl. loopback bypass)
│       │   ├── routes.py      #   /auth/login /auth/callback /auth/logout /auth/me
│       │   └── local.py       #   POST /auth/local (bcrypt fallback user)
│       ├── models.py          # SQLAlchemy ORM (sources/entries/interactions/...)
│       ├── schemas.py         # Pydantic response shapes
│       ├── scheduler.py       # APScheduler + ingest pipeline (incl. embed backfill)
│       ├── embeddings.py      # sentence-transformers singleton
│       ├── llm/               # provider abstraction (Anthropic / OpenAI / Groq / Ollama)
│       ├── scoring/           # recency (per-category) + source + personal + composite
│       ├── sources/
│       │   ├── base.py            # SourcePlugin ABC
│       │   ├── rss.py             # BBC News (phase 1; phase 3: UA + https fix)
│       │   ├── hn.py              # Hacker News top stories
│       │   ├── github_releases.py # GitHub releases (5 repos, ETag-cached)
│       │   ├── nvd.py             # NVD recent CVEs (7-day rolling)
│       │   ├── cisa_kev.py        # CISA Known Exploited Vulnerabilities
│       │   ├── wikipedia_on_this_day.py  # Wikipedia On This Day events
│       │   └── __init__.py        # @register_source + plugin discovery
│       └── routes/
│           ├── health.py
│           ├── sources.py
│           ├── entries.py
│           ├── foryou.py      # /api/foryou — personal top-N
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
            ├── Hamburger.tsx
            ├── LoginPage.tsx   # shown when OIDC is on and user is logged out
            └── UserBadge.tsx   # name + sign-out chip in the header
```

## Phase roadmap

- **Phase 1**: scaffold, schema, one RSS source, minimal UI. ✅
- **Phase 2**: composite scoring engine, sentence-transformers embeddings,
  LLM provider abstraction, For You feed with convergence boost. ✅
- **Phase 3**: six source plugins — BBC, Wikipedia On This Day, Hacker
  News, GitHub releases (5 repos), NVD CVEs, CISA KEV. BBC RSS fetcher
  fixed (proper User-Agent + https). New `tech` and `vulns` categories.
  ✅
- **Phase 4**: The Brief generator (daily + manual + convergence
  triggers), notifications via Apprise (preferred) / Pushover (fallback),
  alert paths for high-CVSS CVEs and convergence clusters. ✅

## Environment variables

All optional except the database credentials (defaults work). See `.env.example` for the full list. The frontend's `VITE_BACKEND_URL` (in `docker-compose.yml`) points Vite's dev-server proxy at the backend container.

## Development notes

- **Hot reload**: both backend (`uvicorn --reload`) and frontend (Vite HMR) reload on file changes via the bind mounts in `docker-compose.yml`.
- **Schema changes**: edit `app/models.py`, then `docker compose exec backend alembic revision --autogenerate -m "..."`. Commit the new file in `alembic/versions/`.
- **Reset**: `docker compose down -v` wipes the postgres volume. Next `up` re-runs migrations from scratch.

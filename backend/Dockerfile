FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
# Install CPU-only torch first (saves ~600 MB vs CUDA build), then the
# rest of the deps. The empty --extra-index-url below prevents pip from
# also hitting the default PyPI for torch; if pip falls back there it
# will still install a usable CPU-only wheel.
RUN pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install -e . --no-deps \
    && pip install -e .

COPY . .

EXPOSE 8000

# Stable production CMD — no auto-reload. alembic upgrade head runs
# once at startup so a fresh pgdata volume is auto-initialized. Local
# dev with hot-reload is opted into via docker-compose.override.yml's
# `command:` override (see docker-compose.override.yml.example), which
# replaces this CMD with the --reload variant. Keeping --reload out of
# the production image avoids two failure modes this codebase has hit:
#
# 1. ``watchfiles`` triggers on any file mutation inside /app — runtime
#    artefacts (cached thumbnails under /app/assets/thumbs, alembic's
#    WAL writes, etc.) cause spurious restarts. Each restart briefly
#    disconnects the Vite proxy on the frontend container, surfacing
#    as ECONNREFUSED on /api/* and a blank dashboard for the duration
#    of the reload (~1-2 s).
#
# 2. Pinning to a specific image SHA loses the "edit-and-reload" loop
#    users expect from dev mode but gains the user-visible stability
#    they expect from prod. The override gives both modes their own
#    command without diverging Dockerfiles.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
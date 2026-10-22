# --- Build stage ------------------------------------------------------------
FROM python:3.12.13-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /venv
ENV PATH=/venv/bin:$PATH
# Own directory: ``pip install .`` from / makes setuptools scan the whole
# filesystem (including /proc) for packages and never finishes.
WORKDIR /build

# CPU-only torch (the PyPI build bundles ~2 GB of CUDA libraries). Kept out
# of requirements.lock because it comes from the PyTorch index; bump it
# together with scripts/lock.sh.
ARG TORCH_VERSION=2.14.1
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch==${TORCH_VERSION}"

COPY requirements.lock ./
RUN pip install -r requirements.lock

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-deps .

# --- Runtime stage ----------------------------------------------------------
FROM python:3.12.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/venv/bin:$PATH

RUN useradd --uid 10001 --create-home app
COPY --from=build /venv /venv

WORKDIR /app
COPY alembic.ini ./
COPY alembic ./alembic
# Mount points for the assets and model-cache volumes; a fresh named
# volume inherits this ownership.
RUN mkdir -p /app/assets /home/app/.cache && chown app:app /app/assets /home/app/.cache

USER app
EXPOSE 8000

# Migrations run at startup so a fresh database volume is initialised.
# Dev hot-reload overrides this command in docker-compose.override.yml.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

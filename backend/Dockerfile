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

# uvicorn with reload for dev; alembic upgrade head runs once at startup so
# a fresh pgdata volume is auto-initialized.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"]
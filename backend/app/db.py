"""Async SQLAlchemy engine + session factory."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    # Recycle connections after 30 min. PostgreSQL silently drops
    # idle connections older than ``tcp_keepalives_idle`` (default
    # 2h on Linux) but a pgbouncer / cloud-native proxy in front
    # often sets the timeout to minutes. Without pool_recycle the
    # pool hands out a stale connection on first use after the
    # timeout, the query fails with ``OperationalError: server
    # closed the connection unexpectedly``, and the request 500s.
    # 30 min is well below the typical proxy timeout (10-60 min)
    # and well above the median inter-request gap, so the
    # recycling cost is negligible.
    pool_recycle=1800,
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a session and ensures it closes."""
    async with SessionLocal() as session:
        yield session


async def dispose_engine() -> None:
    """Tear down the SQLAlchemy engine pool. Called from FastAPI
    lifespan exit so connection pools don't leak across
    ``uvicorn --reload`` cycles."""
    await engine.dispose()
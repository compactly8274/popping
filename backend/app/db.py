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
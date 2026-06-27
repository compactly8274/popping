"""Async Redis client factory + FastAPI dependency."""

import redis.asyncio as redis
from redis.asyncio import Redis

from app.config import settings

_client: Redis | None = None


async def init_redis() -> Redis:
    """Create the singleton Redis client. Idempotent."""
    global _client
    if _client is None:
        _client = redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def get_redis() -> Redis:
    """FastAPI dependency. Falls back to a fresh client if init was skipped."""
    if _client is None:
        return await init_redis()
    return _client
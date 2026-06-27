"""FastAPI dependency providers."""

from redis.asyncio import Redis

from app.redis import get_redis


async def redis_client() -> Redis:
    return await get_redis()
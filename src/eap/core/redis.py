"""Redis client: a single connection pool per process, shared by rate limiting, queues and caches."""

from redis.asyncio import Redis, from_url

from eap.core.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = from_url(str(get_settings().redis_url), decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None

from __future__ import annotations

from app.infrastructure.cache.redis_client import cache_get, get_redis

_RATE_PREFIX = "rl:"


def _redis_key(key: str) -> str:
    return f"{_RATE_PREFIX}{key}"


def is_rate_limited(key: str, *, limit: int, window_seconds: int) -> bool:
    """Increment counter and return True when limit is exceeded."""
    if limit <= 0:
        return False
    client = get_redis()
    redis_key = _redis_key(key)
    count = client.incr(redis_key)
    if count == 1:
        client.expire(redis_key, window_seconds)
    return count > limit


def rate_limit_exceeded(key: str, *, limit: int) -> bool:
    """Return True if key is already at or over limit (no increment)."""
    if limit <= 0:
        return False
    raw = cache_get(_redis_key(key))
    return raw is not None and int(raw) >= limit


def rate_limit_record(key: str, *, window_seconds: int) -> None:
    """Record one failed attempt."""
    is_rate_limited(key, limit=10**9, window_seconds=window_seconds)

from __future__ import annotations

import logging
from typing import Any, Optional

import redis
from redis.exceptions import RedisError

from app.config import settings

log = logging.getLogger(__name__)

_redis_client: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return _redis_client


def redis_ping() -> bool:
    try:
        return bool(get_redis().ping())
    except RedisError as exc:
        log.warning("Redis ping failed: %s", exc)
        return False


def cache_set(key: str, value: str, ttl_seconds: Optional[int] = None) -> None:
    client = get_redis()
    if ttl_seconds:
        client.setex(key, ttl_seconds, value)
    else:
        client.set(key, value)


def cache_get(key: str) -> Optional[str]:
    return get_redis().get(key)


def cache_delete(key: str) -> None:
    get_redis().delete(key)


def tenant_cache_key(tenant_id: str, suffix: str) -> str:
    return f"tenant:{tenant_id}:{suffix}"


def publish_event(channel: str, message: str) -> Any:
    return get_redis().publish(channel, message)

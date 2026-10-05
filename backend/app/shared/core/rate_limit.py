from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from fastapi import HTTPException, status

from app.config import settings
from app.infrastructure.cache.redis_client import cache_get, get_redis

log = logging.getLogger(__name__)

_RATE_PREFIX = "rl:"
TOO_MANY_MESSAGE = "Demasiados intentos. Espera unos minutos e intenta de nuevo."


def _redis_key(key: str) -> str:
    return f"{_RATE_PREFIX}{key}"


def client_ip_from(headers: Mapping[str, str], client_host: Optional[str]) -> str:
    """IP real del cliente.

    Detrás de Caddy, uvicorn ya resuelve X-Forwarded-For (FORWARDED_ALLOW_IPS). Con
    Cloudflare delante, Caddy ve la IP de Cloudflare: se usa CF-Connecting-IP solo si se
    confía explícitamente en Cloudflare (el origen debe aceptar solo sus rangos).
    """
    if settings.trust_cloudflare_ip:
        cf_ip = (headers.get("cf-connecting-ip") or "").strip()
        if cf_ip:
            return cf_ip
    return client_host or "unknown"


def client_ip(request: Any) -> str:
    client = getattr(request, "client", None)
    return client_ip_from(request.headers, client.host if client else None)


def is_rate_limited(key: str, *, limit: int, window_seconds: int) -> bool:
    """Increment counter and return True when limit is exceeded."""
    if limit <= 0:
        return False
    client = get_redis()
    redis_key = _redis_key(key)
    pipe = client.pipeline()
    pipe.incr(redis_key)
    pipe.ttl(redis_key)
    count, ttl = pipe.execute()
    if ttl is None or int(ttl) < 0:
        client.expire(redis_key, window_seconds)
    return int(count) > limit


def rate_limit_exceeded(key: str, *, limit: int) -> bool:
    """Return True if key is already at or over limit (no increment)."""
    if limit <= 0:
        return False
    raw = cache_get(_redis_key(key))
    return raw is not None and int(raw) >= limit


def rate_limit_record(key: str, *, window_seconds: int) -> None:
    """Record one failed attempt."""
    is_rate_limited(key, limit=10**9, window_seconds=window_seconds)


def consume_once(key: str, *, ttl_seconds: int) -> bool:
    """Marca `key` como usada. False si ya se usó. Si Redis cae, deja pasar."""
    if ttl_seconds <= 0:
        return True
    try:
        return bool(get_redis().set(f"once:{key}", "1", nx=True, ex=ttl_seconds))
    except Exception:
        log.warning("consume_once sin Redis para %s", key)
        return True


def enforce_rate_limit(
    key: str,
    *,
    limit: int,
    window_seconds: int,
    message: str = TOO_MANY_MESSAGE,
) -> None:
    """Cuenta un intento y responde 429 al pasar el límite. Si Redis falla, deja pasar."""
    if not settings.rate_limit_enabled:
        return
    try:
        exceeded = is_rate_limited(key, limit=limit, window_seconds=window_seconds)
    except Exception:
        log.warning("Rate limit sin Redis para %s", key)
        return
    if exceeded:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=message,
            headers={"Retry-After": str(window_seconds)},
        )

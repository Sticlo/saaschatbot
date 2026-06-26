from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.infrastructure.cache.redis_client import get_redis

_TRACE_LIMIT = 40


def _trace_key(tenant_id: uuid.UUID | str) -> str:
    return f"sync:webhook_trace:{tenant_id}"


def _last_key(tenant_id: uuid.UUID | str) -> str:
    return f"sync:last_webhook_at:{tenant_id}"


def record_webhook_event(
    tenant_id: uuid.UUID,
    *,
    event: str,
    result: str,
    detail: str = "",
    message_id: str = "",
    instance: str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "at": now,
        "event": event,
        "result": result,
        "detail": detail[:300],
        "message_id": message_id,
        "instance": instance,
    }
    try:
        client = get_redis()
        key = _trace_key(tenant_id)
        client.lpush(key, json.dumps(entry))
        client.ltrim(key, 0, _TRACE_LIMIT - 1)
        client.set(_last_key(tenant_id), now, ex=86_400)
    except Exception:
        pass


def list_webhook_trace(tenant_id: uuid.UUID, *, limit: int = 20) -> list[dict[str, Any]]:
    try:
        client = get_redis()
        raw = client.lrange(_trace_key(tenant_id), 0, max(0, limit - 1))
        out: list[dict[str, Any]] = []
        for item in raw:
            try:
                out.append(json.loads(item))
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return []


def last_webhook_at(tenant_id: uuid.UUID) -> Optional[str]:
    try:
        value = get_redis().get(_last_key(tenant_id))
        return str(value) if value else None
    except Exception:
        return None

from __future__ import annotations

import logging
import uuid

from app.application.sync.history_sync_tracker import reset_history_sync_tracker
from app.infrastructure.cache.redis_client import get_redis

log = logging.getLogger(__name__)


def clear_tenant_sync_state(tenant_id: uuid.UUID) -> None:
    """Limpia locks/cursors de sync al desvincular (sesión desechable)."""
    reset_history_sync_tracker(tenant_id)
    keys = [
        f"tenant:{tenant_id}:sync_running",
        f"tenant:{tenant_id}:sync_debounce",
        f"tenant:{tenant_id}:live_pull_since_ts",
        f"webhook:ensure:{tenant_id}",
    ]
    try:
        redis = get_redis()
        for key in keys:
            redis.delete(key)
    except Exception as exc:
        log.debug("clear_tenant_sync_state tenant=%s: %s", tenant_id, exc)

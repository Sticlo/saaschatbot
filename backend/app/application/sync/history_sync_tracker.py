from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Optional

from app.infrastructure.cache.redis_client import get_redis

log = logging.getLogger(__name__)

_HISTORY_QUIET_SECONDS = 12.0
_timers: dict[str, threading.Timer] = {}
_timers_lock = threading.Lock()


def _history_key(tenant_id: uuid.UUID) -> str:
    return f"tenant:{tenant_id}:history_sync"


def _cancel_quiet_timer(tenant_id: uuid.UUID) -> None:
    key = str(tenant_id)
    with _timers_lock:
        timer = _timers.pop(key, None)
    if timer is not None:
        timer.cancel()


def _schedule_quiet_finalize(tenant_id: uuid.UUID) -> None:
    _cancel_quiet_timer(tenant_id)

    def _fire() -> None:
        with _timers_lock:
            _timers.pop(str(tenant_id), None)
        finalize_history_sync(tenant_id, reason="quiet")

    timer = threading.Timer(_HISTORY_QUIET_SECONDS, _fire)
    timer.daemon = True
    with _timers_lock:
        _timers[str(tenant_id)] = timer
    timer.start()


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _extract_history_meta(payload: dict) -> tuple[bool, Optional[int], int]:
    """Lee isLatest/progress del webhook (Evolution >= 2.3.7 los manda fuera de data)."""
    data = payload.get("data")
    is_latest = _coerce_bool(
        payload.get("isLatest")
        or payload.get("is_latest")
        or (data.get("isLatest") if isinstance(data, dict) else None)
    )
    progress_raw = payload.get("progress")
    if progress_raw is None and isinstance(data, dict):
        progress_raw = data.get("progress")
    progress: Optional[int] = None
    if progress_raw is not None:
        try:
            progress = int(progress_raw)
        except (TypeError, ValueError):
            progress = None

    data = payload.get("data") or []
    if isinstance(data, list):
        batch_size = len(data)
    elif isinstance(data, dict):
        nested = data.get("messages")
        batch_size = len(nested) if isinstance(nested, list) else 1
    else:
        batch_size = 0
    return is_latest, progress, batch_size


def record_messages_set_batch(tenant_id: uuid.UUID, payload: dict) -> None:
    """Registra un lote de historial y notifica al panel cuando termina."""
    is_latest, progress, batch_size = _extract_history_meta(payload)
    try:
        redis = get_redis()
        key = _history_key(tenant_id)
        redis.hincrby(key, "batches", 1)
        if batch_size:
            redis.hincrby(key, "messages", batch_size)
        if progress is not None:
            redis.hset(key, "progress", str(progress))
        redis.expire(key, 86400)
    except Exception as exc:
        log.debug("history_sync tracker redis tenant=%s: %s", tenant_id, exc)

    if progress is not None and progress < 100 and not is_latest:
        try:
            from app.application.realtime.realtime_service import publish_panel_event

            publish_panel_event(
                tenant_id,
                {
                    "type": "sync.progress",
                    "status": "running",
                    "phase": "history",
                    "progress": progress,
                },
            )
        except Exception:
            pass

    if is_latest:
        finalize_history_sync(tenant_id, reason="isLatest")
        return

    _schedule_quiet_finalize(tenant_id)


def finalize_history_sync(tenant_id: uuid.UUID, *, reason: str) -> None:
    """Cierra la fase de historial y dispara un sync incremental ligero."""
    _cancel_quiet_timer(tenant_id)

    stats: dict[str, Any] = {"phase": "history", "reason": reason}
    try:
        redis = get_redis()
        key = _history_key(tenant_id)
        raw = redis.hgetall(key) or {}
        if raw:
            stats["history_batches"] = int(raw.get("batches") or 0)
            stats["history_messages"] = int(raw.get("messages") or 0)
            if raw.get("progress"):
                stats["progress"] = int(raw["progress"])
    except Exception:
        pass

    try:
        from app.application.realtime.realtime_service import publish_panel_event

        publish_panel_event(
            tenant_id,
            {"type": "sync.completed", "status": "completed", **stats},
        )
    except Exception:
        pass

    try:
        from app.application.sync.sync_scheduler import schedule_whatsapp_sync

        schedule_whatsapp_sync(
            tenant_id,
            wait_for_history=False,
            import_agenda=False,
            silent=True,
            debounce=True,
            delay_seconds=2,
        )
    except Exception:
        pass

    log.info("Historial Evolution completado tenant=%s reason=%s", tenant_id, reason)


def reset_history_sync_tracker(tenant_id: uuid.UUID) -> None:
    _cancel_quiet_timer(tenant_id)
    try:
        get_redis().delete(_history_key(tenant_id))
    except Exception:
        pass

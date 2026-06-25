from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.domain.entities.enums import SendQueueStatus

log = logging.getLogger(__name__)

_worker_thread: Optional[threading.Thread] = None
_worker_stop = threading.Event()

PAUSE_KEY_SUFFIX = "outbound:paused"


def is_outbound_paused(tenant_id: uuid.UUID) -> bool:
    from app.infrastructure.cache.redis_client import get_redis, tenant_cache_key

    return bool(get_redis().get(tenant_cache_key(str(tenant_id), PAUSE_KEY_SUFFIX)))


def set_outbound_paused(tenant_id: uuid.UUID, paused: bool) -> None:
    from app.infrastructure.cache.redis_client import get_redis, tenant_cache_key

    key = tenant_cache_key(str(tenant_id), PAUSE_KEY_SUFFIX)
    if paused:
        get_redis().set(key, "1")
    else:
        get_redis().delete(key)


def _worker_loop() -> None:
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import SendQueueItem
    from app.application.outbound.outbound_service import process_send_queue_item

    log.info("Outbound bait worker started")
    poll = settings.outbound_worker_poll_seconds

    while not _worker_stop.is_set():
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            item = (
                db.query(SendQueueItem)
                .filter(
                    SendQueueItem.status == SendQueueStatus.PENDING.value,
                    SendQueueItem.scheduled_at <= now,
                )
                .order_by(SendQueueItem.scheduled_at.asc())
                .with_for_update(skip_locked=True)
                .first()
            )
            if item is None:
                db.close()
                _worker_stop.wait(poll)
                continue

            tenant_id = item.tenant_id
            process_send_queue_item(db, item)
            db.commit()

            from app.application.realtime.realtime_service import publish_panel_event

            publish_panel_event(
                tenant_id,
                {"type": "outbound.progress", "item_id": str(item.id), "status": item.status},
            )
        except Exception:
            log.exception("Outbound worker error")
            db.rollback()
        finally:
            db.close()

    log.info("Outbound bait worker stopped")


def start_outbound_worker() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        name="outbound-bait-worker",
        daemon=True,
    )
    _worker_thread.start()


def stop_outbound_worker() -> None:
    _worker_stop.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=5)

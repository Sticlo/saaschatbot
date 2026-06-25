from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Optional

from app.infrastructure.cache.redis_client import get_redis

log = logging.getLogger(__name__)

WHATSAPP_SYNC_QUEUE = "queue:whatsapp_sync"

_worker_thread: Optional[threading.Thread] = None
_worker_stop = threading.Event()


def enqueue_whatsapp_sync_job(
    *,
    tenant_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
    ip_address: Optional[str] = None,
    wait_for_history: bool = True,
    import_agenda: Optional[bool] = None,
    silent: bool = False,
) -> None:
    payload = {
        "tenant_id": str(tenant_id),
        "user_id": str(user_id) if user_id else None,
        "ip_address": ip_address,
        "wait_for_history": wait_for_history,
        "import_agenda": import_agenda,
        "silent": silent,
    }
    get_redis().lpush(WHATSAPP_SYNC_QUEUE, json.dumps(payload))


def _worker_loop() -> None:
    from app.application.sync.sync_scheduler import run_sync_job

    log.info("WhatsApp sync worker started")
    while not _worker_stop.is_set():
        try:
            item = get_redis().brpop(WHATSAPP_SYNC_QUEUE, timeout=2)
            if not item:
                continue
            _, raw = item
            data = json.loads(raw)
            tenant_id = uuid.UUID(data["tenant_id"])
            user_id = uuid.UUID(data["user_id"]) if data.get("user_id") else None
            import_agenda = data.get("import_agenda")
            if import_agenda is not None:
                import_agenda = bool(import_agenda)
            run_sync_job(
                tenant_id,
                user_id=user_id,
                ip_address=data.get("ip_address"),
                wait_for_history=bool(data.get("wait_for_history", True)),
                import_agenda=import_agenda,
                silent=bool(data.get("silent", False)),
            )
        except Exception:
            log.exception("WhatsApp sync worker error")
    log.info("WhatsApp sync worker stopped")


def start_sync_worker() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        name="whatsapp-sync-worker",
        daemon=True,
    )
    _worker_thread.start()


def stop_sync_worker() -> None:
    _worker_stop.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=1)


def sync_worker_is_alive() -> bool:
    return bool(_worker_thread and _worker_thread.is_alive())

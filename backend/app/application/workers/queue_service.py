from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any, Optional

from app.infrastructure.cache.redis_client import get_redis

log = logging.getLogger(__name__)

INBOUND_WEBHOOK_QUEUE = "queue:inbound_webhooks"
WEBHOOK_DEDUP_TTL_SECONDS = 86_400  # 24h

_worker_thread: Optional[threading.Thread] = None
_worker_stop = threading.Event()


def enqueue_webhook(tenant_id: uuid.UUID, payload: dict) -> None:
    item = json.dumps({"tenant_id": str(tenant_id), "payload": payload})
    get_redis().lpush(INBOUND_WEBHOOK_QUEUE, item)


def webhook_dedup_key(tenant_id: uuid.UUID, dedup_id: str) -> str:
    return f"webhook:dedup:{tenant_id}:{dedup_id}"


def is_duplicate_webhook(tenant_id: uuid.UUID, dedup_id: str) -> bool:
    if not dedup_id:
        return False
    key = webhook_dedup_key(tenant_id, dedup_id)
    client = get_redis()
    if client.set(key, "1", nx=True, ex=WEBHOOK_DEDUP_TTL_SECONDS):
        return False
    return True


def build_dedup_id(payload: dict) -> str:
    event = (payload.get("event") or "unknown").lower()
    data = payload.get("data") or payload

    candidates: list[dict] = []
    if isinstance(data, list):
        candidates = [item for item in data if isinstance(item, dict)]
    elif isinstance(data, dict):
        nested = data.get("messages")
        if isinstance(nested, list):
            candidates = [item for item in nested if isinstance(item, dict)]
        else:
            candidates = [data]

    for item in candidates:
        key = item.get("key") if isinstance(item.get("key"), dict) else {}
        msg_id = key.get("id")
        if msg_id:
            return f"{event}:{msg_id}"
        state = item.get("state") or item.get("status")
        if state is not None:
            return f"{event}:{state}"

    if isinstance(data, dict):
        key = data.get("key") if isinstance(data.get("key"), dict) else {}
        msg_id = key.get("id")
        if msg_id:
            return f"{event}:{msg_id}"
        state = data.get("state") or data.get("status")
        if state is not None:
            return f"{event}:{state}"

    instance = payload.get("instance") or payload.get("instanceName") or ""
    return f"{event}:{instance}"


def _worker_loop() -> None:
    from app.application.messaging.webhook_processor import process_evolution_webhook

    log.info("Webhook worker started")
    while not _worker_stop.is_set():
        try:
            item = get_redis().brpop(INBOUND_WEBHOOK_QUEUE, timeout=2)
            if not item:
                continue
            _, raw = item
            data = json.loads(raw)
            tenant_id = uuid.UUID(data["tenant_id"])
            process_evolution_webhook(tenant_id, data["payload"])
        except Exception:
            log.exception("Webhook worker error")
    log.info("Webhook worker stopped")


def start_webhook_worker() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        name="webhook-worker",
        daemon=True,
    )
    _worker_thread.start()


def stop_webhook_worker() -> None:
    _worker_stop.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=1)

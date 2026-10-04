from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Optional

from app.config import settings
from app.infrastructure.cache.redis_client import get_redis

log = logging.getLogger(__name__)

HEARTBEAT_PREFIX = "worker:heartbeat:"
HEARTBEAT_TTL_SECONDS = 30
AI_SLOT_PREFIX = "ai:slot:"
AI_SLOT_LEASE_SECONDS = 300


def worker_heartbeat_key(kind: str, worker_id: str) -> str:
    return f"{HEARTBEAT_PREFIX}{kind}:{worker_id}"


def touch_heartbeat(kind: str, worker_id: str) -> None:
    get_redis().set(
        worker_heartbeat_key(kind, worker_id),
        str(os.getpid()),
        ex=HEARTBEAT_TTL_SECONDS,
    )


def count_active_workers(kind: str) -> int:
    client = get_redis()
    pattern = f"{HEARTBEAT_PREFIX}{kind}:*"
    count = 0
    for _ in client.scan_iter(match=pattern, count=100):
        count += 1
    return count


def start_heartbeat_loop(
    kind: str,
    worker_id: str,
    stop: threading.Event,
    *,
    interval: float = 10.0,
) -> threading.Thread:
    def _run() -> None:
        while not stop.is_set():
            try:
                touch_heartbeat(kind, worker_id)
            except Exception:
                log.debug("heartbeat failed kind=%s", kind, exc_info=True)
            stop.wait(interval)

    thread = threading.Thread(
        target=_run,
        name=f"heartbeat-{kind}-{worker_id}",
        daemon=True,
    )
    thread.start()
    return thread


def acquire_ai_slot(*, wait_seconds: float = 120.0, poll: float = 0.25) -> Optional[int]:
    """Reserva un slot global de IA (máx. ai_max_parallel_jobs en toda la plataforma)."""
    client = get_redis()
    deadline = time.monotonic() + wait_seconds
    owner = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"

    while time.monotonic() < deadline:
        for slot in range(settings.ai_max_parallel_jobs):
            key = f"{AI_SLOT_PREFIX}{slot}"
            if client.set(key, owner, nx=True, ex=AI_SLOT_LEASE_SECONDS):
                return slot
        time.sleep(poll)
    return None


def refresh_ai_slot(slot: int) -> None:
    key = f"{AI_SLOT_PREFIX}{slot}"
    client = get_redis()
    if client.exists(key):
        client.expire(key, AI_SLOT_LEASE_SECONDS)


def release_ai_slot(slot: Optional[int]) -> None:
    if slot is None:
        return
    try:
        get_redis().delete(f"{AI_SLOT_PREFIX}{slot}")
    except Exception:
        pass


AI_TENANT_SLOT_PREFIX = "ai:tslot:"


def acquire_tenant_ai_slot(tenant_id: uuid.UUID | str) -> Optional[str]:
    """Reserva uno de los `ai_max_parallel_per_tenant` slots del negocio, sin esperar.

    None = el negocio ya tiene todas sus respuestas en curso: el job vuelve a la cola y
    los slots globales quedan para los demás negocios."""
    client = get_redis()
    owner = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
    for slot in range(max(1, settings.ai_max_parallel_per_tenant)):
        key = f"{AI_TENANT_SLOT_PREFIX}{tenant_id}:{slot}"
        if client.set(key, owner, nx=True, ex=AI_SLOT_LEASE_SECONDS):
            return key
    return None


def release_tenant_ai_slot(key: Optional[str]) -> None:
    if not key:
        return
    try:
        get_redis().delete(key)
    except Exception:
        pass


def count_active_ai_slots() -> int:
    client = get_redis()
    count = 0
    for _ in client.scan_iter(match=f"{AI_SLOT_PREFIX}*", count=64):
        count += 1
    return count

"""Revisa cada minuto lo que el cliente notaría si se cae (base de datos, Redis, colas, workers)
y avisa al dev antes de que alguien se queje."""

from __future__ import annotations

import logging
import threading
from typing import Optional

from sqlalchemy import text

from app.application.monitoring.dev_alerts import alert_dev
from app.config import settings
from app.infrastructure.cache.redis_client import get_redis, redis_ping
from app.infrastructure.persistence.database import SessionLocal

log = logging.getLogger(__name__)

WATCHDOG_INTERVAL_SECONDS = 60
INFRA_ALERT_THROTTLE_SECONDS = 600

_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _check_postgres() -> None:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:
        alert_dev(
            "watchdog:postgres",
            "Postgres no responde",
            f"{type(exc).__name__}: {str(exc)[:300]}\nEl panel y las respuestas IA están fallando.",
            severity="critical",
            throttle_seconds=INFRA_ALERT_THROTTLE_SECONDS,
        )


def _check_queues_and_workers() -> None:
    from app.application.ai.ai_queue_service import AI_DELAYED_QUEUE, AI_REPLY_QUEUE, ai_worker_is_alive
    from app.application.workers.queue_service import INBOUND_WEBHOOK_QUEUE, webhook_worker_is_alive
    from app.application.workers.worker_runtime import count_active_workers

    client = get_redis()
    ai_depth = int(client.llen(AI_REPLY_QUEUE))
    webhook_depth = int(client.llen(INBOUND_WEBHOOK_QUEUE))
    delayed = int(client.zcard(AI_DELAYED_QUEUE))

    if ai_depth > settings.ops_ai_queue_alert_threshold:
        alert_dev(
            "watchdog:ai_queue",
            "Cola de respuestas IA atrasada",
            f"{ai_depth} respuestas esperando (reintentos programados: {delayed}).\n"
            "Los clientes están esperando más de lo normal: sube workers de IA o revisa DeepSeek.",
            severity="warning",
        )
    if webhook_depth > settings.ops_webhook_queue_alert_threshold:
        alert_dev(
            "watchdog:webhook_queue",
            "Cola de mensajes entrantes atrasada",
            f"{webhook_depth} webhooks sin procesar. Los mensajes tardan en aparecer en el panel.",
            severity="warning",
        )

    if settings.embed_workers_in_api:
        ai_alive = ai_worker_is_alive()
        webhook_alive = webhook_worker_is_alive()
    else:
        ai_alive = count_active_workers("ai") + count_active_workers("all") > 0
        webhook_alive = count_active_workers("webhook") + count_active_workers("all") > 0
    if not ai_alive:
        alert_dev(
            "watchdog:no_ai_workers",
            "No hay workers de IA vivos",
            f"Nadie está contestando chats. Cola IA: {ai_depth}. Revisa `docker compose ps` / logs del worker ai.",
            severity="critical",
            throttle_seconds=INFRA_ALERT_THROTTLE_SECONDS,
        )
    if not webhook_alive:
        alert_dev(
            "watchdog:no_webhook_workers",
            "No hay workers de webhooks vivos",
            f"Los mensajes entrantes no se procesan. Cola: {webhook_depth}.",
            severity="critical",
            throttle_seconds=INFRA_ALERT_THROTTLE_SECONDS,
        )


def run_checks() -> None:
    _check_postgres()
    if not redis_ping():
        alert_dev(
            "watchdog:redis",
            "Redis no responde",
            "Colas, rate limit y sesiones revocadas dependen de Redis: las respuestas IA están detenidas.",
            severity="critical",
            throttle_seconds=INFRA_ALERT_THROTTLE_SECONDS,
        )
        return
    try:
        _check_queues_and_workers()
    except Exception:
        log.warning("Watchdog no pudo revisar colas", exc_info=True)


def _loop() -> None:
    while not _stop.wait(WATCHDOG_INTERVAL_SECONDS):
        try:
            run_checks()
        except Exception:
            log.warning("Watchdog de salud falló", exc_info=True)


def start_health_watchdog() -> None:
    global _thread
    if not settings.is_production() or (_thread and _thread.is_alive()):
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="health-watchdog", daemon=True)
    _thread.start()


def stop_health_watchdog() -> None:
    _stop.set()

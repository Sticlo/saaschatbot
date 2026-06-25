from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
import uuid

log = logging.getLogger(__name__)

_stop = threading.Event()


def _handle_signal(*_args) -> None:
    log.info("Señal de parada recibida, cerrando workers…")
    _stop.set()


def _worker_id() -> str:
    return os.environ.get("WORKER_ID") or f"{os.getpid()}-{uuid.uuid4().hex[:6]}"


def _run_webhook(worker_id: str) -> None:
    from app.application.workers.queue_service import start_webhook_worker, stop_webhook_worker
    from app.application.workers.worker_runtime import start_heartbeat_loop

    start_heartbeat_loop("webhook", worker_id, _stop)
    start_webhook_worker()
    log.info("Webhook worker activo id=%s", worker_id)
    _stop.wait()
    stop_webhook_worker()


def _run_outbound(worker_id: str) -> None:
    from app.application.outbound.bait_scheduler import start_outbound_worker, stop_outbound_worker
    from app.application.workers.worker_runtime import start_heartbeat_loop

    start_heartbeat_loop("outbound", worker_id, _stop)
    start_outbound_worker()
    log.info("Outbound worker activo id=%s", worker_id)
    _stop.wait()
    stop_outbound_worker()


def _run_ai(worker_id: str, *, recover: bool) -> None:
    from app.application.ai.ai_queue_service import (
        recover_pending_ai_replies,
        start_ai_worker,
        stop_ai_worker,
    )
    from app.application.workers.worker_runtime import start_heartbeat_loop

    start_heartbeat_loop("ai", worker_id, _stop)
    start_ai_worker()
    if recover:
        recover_pending_ai_replies()
    log.info("AI worker activo id=%s recover=%s", worker_id, recover)
    _stop.wait()
    stop_ai_worker()


def _run_sync(worker_id: str) -> None:
    from app.application.sync.sync_queue_service import start_sync_worker, stop_sync_worker
    from app.application.workers.worker_runtime import start_heartbeat_loop

    start_heartbeat_loop("sync", worker_id, _stop)
    start_sync_worker()
    log.info("WhatsApp sync worker activo id=%s", worker_id)
    _stop.wait()
    stop_sync_worker()


def _run_all(worker_id: str, *, recover_ai: bool) -> None:
    from app.application.ai.ai_queue_service import (
        recover_pending_ai_replies,
        start_ai_worker,
        stop_ai_worker,
    )
    from app.application.outbound.bait_scheduler import start_outbound_worker, stop_outbound_worker
    from app.application.sync.sync_queue_service import start_sync_worker, stop_sync_worker
    from app.application.workers.queue_service import start_webhook_worker, stop_webhook_worker
    from app.application.workers.worker_runtime import start_heartbeat_loop

    start_heartbeat_loop("all", worker_id, _stop)
    start_webhook_worker()
    start_outbound_worker()
    start_ai_worker()
    start_sync_worker()
    if recover_ai:
        recover_pending_ai_replies()
    log.info("Todos los workers activos id=%s", worker_id)
    _stop.wait()
    stop_ai_worker()
    stop_outbound_worker()
    stop_webhook_worker()
    stop_sync_worker()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Workers en background de SaasChatbot (producción / escala)",
    )
    parser.add_argument(
        "kind",
        choices=["webhook", "outbound", "ai", "sync", "all"],
        help="Tipo de worker a ejecutar",
    )
    parser.add_argument(
        "--recover-ai",
        action="store_true",
        help="Reencolar chats IA pendientes al arrancar (solo un proceso AI)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    worker_id = _worker_id()
    os.environ.setdefault("WORKER_ID", worker_id)

    try:
        if args.kind == "webhook":
            _run_webhook(worker_id)
        elif args.kind == "outbound":
            _run_outbound(worker_id)
        elif args.kind == "ai":
            _run_ai(worker_id, recover=args.recover_ai)
        elif args.kind == "sync":
            _run_sync(worker_id)
        else:
            _run_all(worker_id, recover_ai=args.recover_ai)
    except Exception:
        log.exception("Worker terminó con error kind=%s", args.kind)
        return 1

    log.info("Worker %s detenido", args.kind)
    return 0


if __name__ == "__main__":
    sys.exit(main())

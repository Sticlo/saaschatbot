from __future__ import annotations

import logging
import threading
from typing import Optional

from app.application.billing.auto_renew_service import process_auto_renewals
from app.application.billing.subscription_service import expire_lapsed_subscriptions
from app.infrastructure.persistence.database import SessionLocal

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 900

_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _loop() -> None:
    while not _stop.is_set():
        try:
            with SessionLocal() as db:
                renewals = process_auto_renewals(db)
                expired = expire_lapsed_subscriptions(db)
            if any(renewals.values()):
                log.info("Cobro automático: %s", renewals)
            if expired:
                log.info("Suscripciones vencidas marcadas: %s", expired)
        except Exception:
            log.exception("Error revisando suscripciones vencidas")
        _stop.wait(SWEEP_INTERVAL_SECONDS)


def start_subscription_sweeper() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="subscription-sweeper", daemon=True)
    _thread.start()


def stop_subscription_sweeper() -> None:
    _stop.set()

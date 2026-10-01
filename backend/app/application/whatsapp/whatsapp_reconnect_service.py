from __future__ import annotations

import logging
import threading
from typing import Optional

from sqlalchemy.orm import Session

from app.application.whatsapp.whatsapp_gateway import (
    WhatsAppGatewayError,
    connect_instance,
    connection_state,
)
from app.domain.entities import Tenant, WhatsAppSession
from app.domain.entities.enums import WhatsAppStatus
from app.infrastructure.cache.redis_client import cache_get, cache_set, tenant_cache_key
from app.infrastructure.persistence.database import SessionLocal

log = logging.getLogger(__name__)

RECONNECT_INTERVAL_SECONDS = 60
CONNECT_RETRY_SECONDS = 120
# Si WhatsApp pide QR otra vez, el celular cerró la sesión: insistir solo generaría QRs inútiles.
NEEDS_QR_BACKOFF_SECONDS = 30 * 60

_watchdog_thread: Optional[threading.Thread] = None
_watchdog_stop = threading.Event()


def _backoff_key(tenant: Tenant) -> str:
    return tenant_cache_key(str(tenant.id), "wa_reconnect_backoff")


def _gateway_state(payload: dict) -> str:
    instance = payload.get("instance") if isinstance(payload.get("instance"), dict) else {}
    return str(instance.get("state") or payload.get("state") or "").lower()


def _asks_for_qr(payload: dict) -> bool:
    qrcode = payload.get("qrcode") if isinstance(payload.get("qrcode"), dict) else {}
    return bool(payload.get("base64") or payload.get("code") or qrcode.get("base64") or qrcode.get("code"))


def try_reconnect(db: Session, tenant: Tenant, session: WhatsAppSession) -> bool:
    """Restaura una sesión que se cayó (internet, Mac dormido, reinicio) con las credenciales guardadas.
    True = quedó conectada."""
    from app.application.whatsapp.whatsapp_service import refresh_session_status

    if cache_get(_backoff_key(tenant)):
        return False
    try:
        state = _gateway_state(connection_state(session.instance_name))
    except WhatsAppGatewayError:
        return False

    if state == "close":
        try:
            result = connect_instance(session.instance_name)
        except WhatsAppGatewayError as exc:
            log.warning("Reconexión WhatsApp falló tenant=%s: %s", tenant.id, exc)
            cache_set(_backoff_key(tenant), "1", ttl_seconds=CONNECT_RETRY_SECONDS)
            return False
        if isinstance(result, dict) and _asks_for_qr(result):
            log.warning("WhatsApp pide escanear QR de nuevo tenant=%s — el celular cerró la sesión", tenant.id)
            cache_set(_backoff_key(tenant), "1", ttl_seconds=NEEDS_QR_BACKOFF_SECONDS)
            return False
        log.info("Reconexión WhatsApp solicitada tenant=%s", tenant.id)
        return False

    if state != "open":
        return False

    refresh_session_status(db, tenant, session)
    db.commit()
    reconnected = tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value
    if reconnected:
        log.info("WhatsApp reconectado sin QR tenant=%s", tenant.id)
    return reconnected


def _sweep_once() -> None:
    db = SessionLocal()
    try:
        rows = (
            db.query(Tenant, WhatsAppSession)
            .join(WhatsAppSession, WhatsAppSession.tenant_id == Tenant.id)
            .filter(
                Tenant.is_active.is_(True),
                Tenant.whatsapp_status.in_(
                    [WhatsAppStatus.DISCONNECTED.value, WhatsAppStatus.CONNECTING.value]
                ),
                WhatsAppSession.active_connection_id.isnot(None),
                WhatsAppSession.bound_owner_jid.isnot(None),
            )
            .all()
        )
        for tenant, session in rows:
            try:
                try_reconnect(db, tenant, session)
            except Exception:
                db.rollback()
                log.exception("Error reconectando WhatsApp tenant=%s", tenant.id)
    finally:
        db.close()


def _watchdog_loop() -> None:
    log.info("WhatsApp reconnect watchdog started")
    while not _watchdog_stop.wait(RECONNECT_INTERVAL_SECONDS):
        try:
            _sweep_once()
        except Exception:
            log.exception("WhatsApp reconnect watchdog error")
    log.info("WhatsApp reconnect watchdog stopped")


def start_whatsapp_reconnect_watchdog() -> None:
    global _watchdog_thread
    if _watchdog_thread and _watchdog_thread.is_alive():
        return
    _watchdog_stop.clear()
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop,
        name="whatsapp-reconnect-watchdog",
        daemon=True,
    )
    _watchdog_thread.start()


def stop_whatsapp_reconnect_watchdog() -> None:
    _watchdog_stop.set()
    if _watchdog_thread and _watchdog_thread.is_alive():
        _watchdog_thread.join(timeout=1)

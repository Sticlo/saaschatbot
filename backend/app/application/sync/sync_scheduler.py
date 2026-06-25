from __future__ import annotations

import logging
import threading
import uuid
from typing import Optional

log = logging.getLogger(__name__)

_SYNC_LOCK_TTL = 120
_SYNC_DEBOUNCE_SECONDS = 60


def run_sync_job(
    tenant_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
    ip_address: Optional[str] = None,
    *,
    wait_for_history: bool = True,
) -> None:
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Tenant, WhatsAppSession
    from app.infrastructure.cache.redis_client import get_redis
    from app.application.sync.chat_sync_service import sync_whatsapp_chats
    from app.application.realtime.realtime_service import publish_panel_event
    from app.application.billing.tenant_service import log_audit

    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()
        if tenant is None or session is None:
            return
        stats = sync_whatsapp_chats(
            db,
            tenant=tenant,
            session=session,
            wait_for_history=wait_for_history,
        )
        if user_id is not None:
            log_audit(
                db,
                tenant_id=tenant.id,
                user_id=user_id,
                action="whatsapp.chats_synced",
                details=stats,
                ip_address=ip_address,
            )
        db.commit()
        publish_panel_event(
            tenant_id,
            {"type": "sync.completed", **stats, "status": "completed"},
        )
        if (
            wait_for_history
            and stats.get("conversations_imported", 0) == 0
            and stats.get("messages_imported", 0) == 0
        ):
            log.info(
                "Sync sin chats aún tenant=%s — reintento en 30s sin esperar historial",
                tenant_id,
            )
            schedule_whatsapp_sync(
                tenant_id,
                wait_for_history=False,
                delay_seconds=30,
            )
        _schedule_delayed_enrich(tenant_id, delay_seconds=75)
    except Exception as exc:
        log.warning("Sync background falló tenant=%s: %s", tenant_id, exc)
        db.rollback()
        try:
            publish_panel_event(
                tenant_id,
                {"type": "sync.completed", "status": "failed", "message": str(exc)},
            )
        except Exception:
            pass
    finally:
        db.close()
        try:
            get_redis().delete(f"tenant:{tenant_id}:sync_running")
        except Exception:
            pass


def _schedule_delayed_enrich(tenant_id: uuid.UUID, delay_seconds: float = 75) -> None:
    """Re-enrich 75s después del sync: captura contactos que Evolution poblaba mientras sincronizábamos."""
    import time as _time

    def _job() -> None:
        _time.sleep(delay_seconds)
        db2 = None
        try:
            from app.infrastructure.persistence.database import SessionLocal
            from app.domain.entities import Tenant, WhatsAppSession
            from app.domain.entities.enums import WhatsAppStatus
            from app.application.sync.contact_identity_service import enrich_tenant_conversations
            from app.application.realtime.realtime_service import publish_panel_event

            db2 = SessionLocal()
            tenant2 = db2.query(Tenant).filter(Tenant.id == tenant_id).first()
            session2 = (
                db2.query(WhatsAppSession)
                .filter(WhatsAppSession.tenant_id == tenant_id)
                .first()
            )
            if (
                tenant2 is None
                or session2 is None
                or tenant2.whatsapp_status != WhatsAppStatus.CONNECTED.value
                or session2.active_connection_id is None
            ):
                return
            stats2 = enrich_tenant_conversations(db2, tenant=tenant2, session=session2)
            db2.commit()
            if stats2.get("names_fixed") or stats2.get("phones_fixed"):
                log.info(
                    "Re-enrich tardío tenant=%s: %s nombres, %s teléfonos",
                    tenant_id,
                    stats2.get("names_fixed"),
                    stats2.get("phones_fixed"),
                )
                from app.domain.entities import Conversation
                from app.application.realtime.realtime_service import publish_conversation_updated

                rows = (
                    db2.query(Conversation)
                    .filter(
                        Conversation.tenant_id == tenant_id,
                        Conversation.whatsapp_connection_id == session2.active_connection_id,
                    )
                    .all()
                )
                for row in rows:
                    publish_conversation_updated(tenant_id, row)
                publish_panel_event(
                    tenant_id,
                    {"type": "contacts.enriched", **stats2, "status": "completed"},
                )
        except Exception as exc:
            log.debug("Re-enrich tardío falló tenant=%s: %s", tenant_id, exc)
            if db2:
                db2.rollback()
        finally:
            if db2:
                db2.close()

    threading.Thread(target=_job, daemon=True).start()


def schedule_whatsapp_sync(
    tenant_id: uuid.UUID,
    *,
    user_id: Optional[uuid.UUID] = None,
    ip_address: Optional[str] = None,
    wait_for_history: bool = True,
    debounce: bool = False,
    delay_seconds: float = 0,
) -> bool:
    """Encola sync en background. Retorna False si ya hay uno en curso."""
    import time

    from app.infrastructure.cache.redis_client import get_redis

    redis = get_redis()
    lock_key = f"tenant:{tenant_id}:sync_running"
    if not redis.set(lock_key, "1", nx=True, ex=_SYNC_LOCK_TTL):
        log.debug("Sync ya en curso tenant=%s", tenant_id)
        return False

    if debounce:
        debounce_key = f"tenant:{tenant_id}:sync_debounce"
        if not redis.set(debounce_key, "1", nx=True, ex=_SYNC_DEBOUNCE_SECONDS):
            redis.delete(lock_key)
            log.debug("Sync debounced tenant=%s", tenant_id)
            return False

    def _job():
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        run_sync_job(
            tenant_id,
            user_id=user_id,
            ip_address=ip_address,
            wait_for_history=wait_for_history,
        )

    threading.Thread(target=_job, daemon=True).start()
    try:
        from app.application.realtime.realtime_service import publish_panel_event

        publish_panel_event(
            tenant_id,
            {"type": "sync.started", "status": "running"},
        )
    except Exception:
        pass
    log.info(
        "Sync programado tenant=%s wait_history=%s delay=%ss",
        tenant_id,
        wait_for_history,
        delay_seconds,
    )
    return True


def ensure_whatsapp_sync_after_connect(tenant_id: uuid.UUID, *, force: bool = False) -> bool:
    """Dispara sync tras QR/conexión. Siempre sincroniza al conectar."""
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Tenant, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus

    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()
        if (
            tenant is None
            or session is None
            or tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value
            or session.active_connection_id is None
        ):
            return False

        return schedule_whatsapp_sync(
            tenant_id,
            wait_for_history=True,
            delay_seconds=3,
        )
    finally:
        db.close()

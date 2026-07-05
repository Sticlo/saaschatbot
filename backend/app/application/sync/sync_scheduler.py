from __future__ import annotations

import logging
import threading
import uuid
from typing import Optional

log = logging.getLogger(__name__)

_SYNC_LOCK_TTL = 300
_SYNC_DEBOUNCE_SECONDS = 60
_SYNC_DEBOUNCE_FAST_SECONDS = 3


def run_sync_job(
    tenant_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
    ip_address: Optional[str] = None,
    *,
    wait_for_history: bool = True,
    import_agenda: Optional[bool] = None,
    silent: bool = False,
) -> None:
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Tenant, WhatsAppSession
    from app.infrastructure.cache.redis_client import get_redis
    from app.application.realtime.realtime_service import publish_panel_event
    from app.application.billing.tenant_service import log_audit
    from app.application.chatwoot.chatwoot_service import (
        chatwoot_sync_mode,
        ensure_chatwoot_integration,
    )

    if import_agenda is None:
        import_agenda = not wait_for_history

    db = SessionLocal()
    stats: dict = {}
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()
        if tenant is None or session is None:
            return

        if chatwoot_sync_mode():
            ensure_chatwoot_integration(db, tenant=tenant, session=session)
            from app.application.chatwoot.chatwoot_inbox_sync import sync_chatwoot_inbox

            stats = sync_chatwoot_inbox(db, tenant=tenant, session=session)
            stats["source"] = "chatwoot"
            stats["status"] = "completed"
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
            if not silent:
                publish_panel_event(
                    tenant_id,
                    {"type": "sync.completed", **stats},
                )
            return

        from app.application.sync.chat_sync_service import sync_whatsapp_chats

        stats = sync_whatsapp_chats(
            db,
            tenant=tenant,
            session=session,
            wait_for_history=wait_for_history,
            import_agenda=import_agenda,
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
        if not silent:
            publish_panel_event(
                tenant_id,
                {"type": "sync.completed", **stats, "status": "completed"},
            )
        if (
            stats.get("conversations_imported", 0) == 0
            and stats.get("messages_imported", 0) == 0
            and not wait_for_history
        ):
            for retry_delay in (20, 45):
                schedule_whatsapp_sync(
                    tenant_id,
                    wait_for_history=False,
                    delay_seconds=retry_delay,
                    silent=True,
                )
        if not silent:
            _schedule_delayed_enrich(tenant_id, delay_seconds=45)
    except Exception as exc:
        log.warning("Sync background falló tenant=%s: %s", tenant_id, exc)
        db.rollback()
        if not silent:
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


def _schedule_delayed_enrich(tenant_id: uuid.UUID, delay_seconds: float = 45) -> None:
    """Re-enrich tras sync custom (omitido en modo Chatwoot)."""
    from app.application.chatwoot.chatwoot_service import chatwoot_sync_mode

    if chatwoot_sync_mode():
        return
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


def _schedule_contact_name_enrich(tenant_id: uuid.UUID, *, delay_seconds: float = 12) -> None:
    """Aplica nombres de WhatsApp (pushName/agenda) tras conectar — solo sync custom."""
    from app.application.chatwoot.chatwoot_service import chatwoot_sync_mode

    if chatwoot_sync_mode():
        return
    import time as _time

    def _job() -> None:
        _time.sleep(delay_seconds)
        db2 = None
        try:
            from app.infrastructure.persistence.database import SessionLocal
            from app.domain.entities import Tenant, WhatsAppSession
            from app.domain.entities.enums import WhatsAppStatus
            from app.application.sync.contact_identity_service import enrich_tenant_conversations
            from app.application.sync.contact_name_cache_service import (
                backfill_names_from_evolution_api,
            )
            from app.application.realtime.realtime_service import publish_panel_event
            from app.application.whatsapp.whatsapp_status import build_owner_display_names

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
            owner_names = build_owner_display_names(session2)
            from app.application.sync.contact_name_cache_service import (
                backfill_names_from_evolution_db,
            )
            backfill_names_from_evolution_db(
                session2.instance_name, owner_names=owner_names
            )
            backfill_names_from_evolution_api(
                session2.instance_name, max_pages=20, owner_names=owner_names
            )
            stats = enrich_tenant_conversations(
                db2, tenant=tenant2, session=session2
            )
            db2.commit()
            if stats.get("names_fixed"):
                publish_panel_event(
                    tenant_id,
                    {"type": "contacts.enriched", **stats, "status": "completed"},
                )
        except Exception as exc:
            log.debug("Enrich nombres tardío falló tenant=%s: %s", tenant_id, exc)
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
    import_agenda: Optional[bool] = None,
    silent: bool = False,
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
        debounce_ttl = (
            _SYNC_DEBOUNCE_FAST_SECONDS if not wait_for_history else _SYNC_DEBOUNCE_SECONDS
        )
        if not redis.set(debounce_key, "1", nx=True, ex=debounce_ttl):
            redis.delete(lock_key)
            log.debug("Sync debounced tenant=%s", tenant_id)
            return False

    def _job():
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        from app.application.sync.sync_queue_service import enqueue_whatsapp_sync_job

        enqueue_whatsapp_sync_job(
            tenant_id=tenant_id,
            user_id=user_id,
            ip_address=ip_address,
            wait_for_history=wait_for_history,
            import_agenda=import_agenda,
            silent=silent,
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
    """Dispara sync tras QR/conexión. Reintenta mientras Evolution importa chats."""
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Conversation, Tenant, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus
    from app.application.conversations.whatsapp_conversation_service import (
        ensure_whatsapp_binding_ready,
    )
    from app.application.whatsapp.whatsapp_service import ensure_evolution_webhook
    from app.config import settings

    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()
        if tenant is None or session is None:
            return False
        if tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value:
            return False

        if session.active_connection_id is None:
            ensure_whatsapp_binding_ready(
                db,
                tenant=tenant,
                session=session,
            )
            db.commit()
            db.refresh(session)

        if session.active_connection_id is None:
            log.warning("Sync post-conexión omitido — sin active_connection_id tenant=%s", tenant_id)
            return False

        try:
            ensure_evolution_webhook(session, tenant.id, force=force)
        except Exception:
            log.warning("ensure_webhook tras conexión tenant=%s", tenant_id, exc_info=True)

        from app.application.chatwoot.chatwoot_service import (
            chatwoot_only_mode,
            chatwoot_sync_mode,
            chatwoot_token_configured,
            ensure_chatwoot_integration,
        )

        import_history = settings.whatsapp_import_history_on_connect

        if chatwoot_only_mode():
            if chatwoot_token_configured():
                ensure_chatwoot_integration(db, tenant=tenant, session=session, force=force)
                db.commit()

            if chatwoot_sync_mode():
                if not import_history:
                    log.info(
                        "Chatwoot inbox sync omitido — sin importar historial tenant=%s",
                        tenant_id,
                    )
                    return True

                from app.application.chatwoot.chatwoot_inbox_sync import sync_chatwoot_inbox

                def _cw_sync():
                    import time

                    for delay in (15, 45, 90, 120):
                        time.sleep(delay)
                        db2 = None
                        try:
                            from app.infrastructure.persistence.database import SessionLocal

                            db2 = SessionLocal()
                            t2 = db2.query(Tenant).filter(Tenant.id == tenant_id).first()
                            s2 = db2.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()
                            if t2 and s2:
                                ensure_chatwoot_integration(db2, tenant=t2, session=s2)
                                db2.commit()
                                stats = sync_chatwoot_inbox(db2, tenant=t2, session=s2)
                                log.info("Chatwoot inbox sync tenant=%s stats=%s", tenant_id, stats)
                                if stats.get("conversations", 0) > 0:
                                    break
                        except Exception as exc:
                            log.warning("Chatwoot inbox sync falló tenant=%s: %s", tenant_id, exc)
                        finally:
                            if db2:
                                db2.close()

                import threading

                threading.Thread(target=_cw_sync, daemon=True).start()
                log.info("Sync Evolution omitido — modo Chatwoot tenant=%s", tenant_id)
                return True

            log.warning(
                "CHATWOOT_ENABLED sin CHATWOOT_API_TOKEN — sync Evolution como fallback tenant=%s",
                tenant_id,
            )

        if not import_history:
            log.info(
                "Sync post-conexión omitido — solo mensajes nuevos tenant=%s",
                tenant_id,
            )
            return True

        conv_count = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant_id,
                Conversation.whatsapp_connection_id == session.active_connection_id,
            )
            .count()
        )

        schedule_whatsapp_sync(tenant_id, wait_for_history=False, delay_seconds=1)
        for delay in (12, 30, 60, 120):
            schedule_whatsapp_sync(
                tenant_id,
                wait_for_history=False,
                delay_seconds=delay,
                silent=True,
            )
        schedule_whatsapp_sync(
            tenant_id,
            wait_for_history=True,
            delay_seconds=45,
            silent=True,
            import_agenda=False,
        )
        _schedule_contact_name_enrich(tenant_id, delay_seconds=8)

        if conv_count == 0:
            log.info("Bootstrap sync automático tenant=%s (0 chats tras conectar)", tenant_id)
        return True
    finally:
        db.close()

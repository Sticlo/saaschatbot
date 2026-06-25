from __future__ import annotations

import logging
import uuid

from app.infrastructure.persistence.database import SessionLocal
from app.domain.entities import Tenant, WhatsAppSession
from app.application.messaging.message_service import (
    handle_connection_update,
    handle_qrcode_update,
    import_evolution_chat_or_contact,
    parse_messages_upsert,
    save_inbound_message,
    save_outbound_from_phone,
    update_message_status,
)
from app.application.workers.queue_service import build_dedup_id, is_duplicate_webhook

log = logging.getLogger(__name__)


def _payload_instance(payload: dict) -> str:
    return str(
        payload.get("instance")
        or payload.get("instanceName")
        or (payload.get("data") or {}).get("instance")
        or ""
    )


def process_evolution_webhook(tenant_id: uuid.UUID, payload: dict) -> None:
    dedup_id = build_dedup_id(payload)
    if is_duplicate_webhook(tenant_id, dedup_id):
        log.debug("Webhook duplicado ignorado tenant=%s id=%s", tenant_id, dedup_id)
        return

    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        session = (
            db.query(WhatsAppSession)
            .filter(WhatsAppSession.tenant_id == tenant_id)
            .first()
        )
        if tenant is None or session is None:
            log.warning("Webhook sin tenant/session tenant=%s", tenant_id)
            return

        instance_name = _payload_instance(payload)
        if instance_name and instance_name != session.instance_name:
            log.warning(
                "Instancia no coincide tenant=%s expected=%s got=%s",
                tenant_id,
                session.instance_name,
                instance_name,
            )
            return

        event = (payload.get("event") or "").lower().replace("_", ".")
        data = payload.get("data") or payload
        pending_ai_jobs: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []

        if event == "connection.update":
            handle_connection_update(db, tenant=tenant, session=session, data=data)
            db.commit()
            from app.domain.entities.enums import WhatsAppStatus

            if tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value:
                from app.application.sync.sync_scheduler import ensure_whatsapp_sync_after_connect

                ensure_whatsapp_sync_after_connect(tenant_id, force=True)
            return
        elif event == "qrcode.updated":
            handle_qrcode_update(db, session, tenant, data if isinstance(data, dict) else {})
        elif event == "messages.upsert":
            connection_id = session.active_connection_id
            if connection_id is None:
                log.debug("Mensaje ignorado — WhatsApp no vinculado tenant=%s", tenant_id)
            else:
                from app.application.messaging.media_cache import save_media_from_webhook

                for item in parse_messages_upsert(data):
                    # Capturar base64 del webhook y guardar en disco
                    b64 = item.get("base64") or ""
                    if b64 and item.get("message_id"):
                        from app.application.messaging.message_service import _detect_media_type_from_body
                        mtype = _detect_media_type_from_body(item["body"])
                        if mtype:
                            save_media_from_webhook(
                                str(tenant_id),
                                item["message_id"],
                                b64,
                                media_type=mtype,
                                mimetype=item.get("mimetype") or "",
                            )

                    if item.get("from_me"):
                        save_outbound_from_phone(
                            db,
                            tenant=tenant,
                            evolution_message_id=item["message_id"],
                            remote_jid=item["remote_jid"],
                            body=item["body"],
                            message_key=item.get("key") if isinstance(item.get("key"), dict) else None,
                            lid_jid=item.get("lid_jid") or "",
                            whatsapp_connection_id=connection_id,
                        )
                    else:
                        msg = save_inbound_message(
                            db,
                            tenant=tenant,
                            evolution_message_id=item["message_id"],
                            remote_jid=item["remote_jid"],
                            body=item["body"],
                            push_name=item.get("push_name") or "",
                            message_key=item.get("key") if isinstance(item.get("key"), dict) else None,
                            lid_jid=item.get("lid_jid") or "",
                            whatsapp_connection_id=connection_id,
                        )
                        if msg is not None:
                            pending_ai_jobs.append((tenant.id, msg.conversation_id, msg.id))
        elif event in ("messages.set",):
            # Historial masivo lo importa el sync automático; evita duplicados con messages.upsert.
            from app.application.sync.sync_scheduler import schedule_whatsapp_sync

            schedule_whatsapp_sync(
                tenant_id,
                wait_for_history=False,
                debounce=True,
                delay_seconds=3,
            )
        elif event in ("chats.set", "chats.upsert"):
            records = data if isinstance(data, list) else [data]
            for record in records:
                if isinstance(record, dict):
                    import_evolution_chat_or_contact(
                        db,
                        tenant=tenant,
                        record=record,
                        whatsapp_connection_id=session.active_connection_id,
                    )
            from app.application.sync.sync_scheduler import schedule_whatsapp_sync

            schedule_whatsapp_sync(
                tenant_id,
                wait_for_history=False,
                debounce=True,
                delay_seconds=3,
            )
        elif event in ("contacts.set", "contacts.upsert"):
            # Los contactos de agenda no son chats; el sync los usa solo para nombres.
            from app.application.sync.sync_scheduler import schedule_whatsapp_sync

            schedule_whatsapp_sync(
                tenant_id,
                wait_for_history=False,
                debounce=True,
                delay_seconds=5,
            )
        elif event == "chats.update":
            records = data if isinstance(data, list) else [data]
            for record in records:
                if not isinstance(record, dict):
                    continue
                import_evolution_chat_or_contact(
                    db,
                    tenant=tenant,
                    record=record,
                    whatsapp_connection_id=session.active_connection_id,
                )
        elif event == "messages.update":
            update_message_status(db, tenant_id, data if isinstance(data, dict) else {})

        if event != "connection.update":
            db.commit()
            if pending_ai_jobs:
                from app.application.ai.ai_queue_service import flush_pending_ai_replies

                flush_pending_ai_replies(pending_ai_jobs)
    except Exception:
        log.exception("Error processing Evolution webhook for tenant %s", tenant_id)
        db.rollback()
    finally:
        db.close()

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

_WEBHOOK_CHATS_BATCH = 40

# Solo importar chats con mensajes de los últimos N días desde chats.set
# (evita crear cientos de conversaciones viejas que se ven como "chats fantasma")
_CHATS_SET_MAX_AGE_DAYS = 90

# Límite máximo de conversaciones totales que se crean desde chats.set.
# Una vez superado, chats.set solo actualiza existentes (no crea nuevas).
_MAX_CONVERSATIONS_FROM_CHATS_SET = 300


def _chat_record_sort_ts(record: dict) -> int:
    for key in ("lastMessageTimestamp", "conversationTimestamp", "updatedAt"):
        raw = record.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


def _chat_record_has_content(record: dict) -> bool:
    """True solo si el chat tiene historial de mensajes real Y es reciente.

    Requiere lastMessageTimestamp > 0 dentro de los últimos _CHATS_SET_MAX_AGE_DAYS.
    Los chats muy viejos o sin timestamp se ignoran para no llenar el panel.
    """
    import time

    min_ts = int(time.time()) - _CHATS_SET_MAX_AGE_DAYS * 86400

    ts_raw = (
        record.get("lastMessageTimestamp")
        or record.get("conversationTimestamp")
    )
    if ts_raw:
        try:
            ts = int(ts_raw)
            if ts > min_ts:
                return True
        except (TypeError, ValueError):
            pass

    # lastMessage con contenido real (sin importar timestamp)
    last_msg = record.get("lastMessage")
    if isinstance(last_msg, dict):
        msg = last_msg.get("message") or {}
        if isinstance(msg, dict) and any(msg.values()):
            # Verificar que el lastMessage también sea reciente
            lm_ts = last_msg.get("messageTimestamp") or last_msg.get("messageStubTimestamp")
            if lm_ts:
                try:
                    if int(lm_ts) > min_ts:
                        return True
                except (TypeError, ValueError):
                    pass

    return False


def _import_chats_progressive(
    db,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    records: list,
) -> int:
    """Actualiza metadatos de chats existentes desde chats.set/upsert.

    NUNCA crea conversaciones nuevas — eso solo ocurre cuando llega un mensaje real
    via messages.upsert o durante el sync completo con mensajes confirmados.
    """
    from app.domain.entities import Conversation
    from app.shared.core.phone import resolve_contact_phone

    connection_id = session.active_connection_id
    if connection_id is None:
        return 0

    sorted_records = sorted(
        [r for r in records if isinstance(r, dict) and _chat_record_has_content(r)],
        key=_chat_record_sort_ts,
        reverse=True,
    )
    if not sorted_records:
        return 0

    updated = 0
    for record in sorted_records[:_WEBHOOK_CHATS_BATCH]:
        remote_jid = str(record.get("remoteJid") or record.get("id") or "")
        if not remote_jid:
            continue

        phone = resolve_contact_phone(remote_jid)
        q = db.query(Conversation).filter(
            Conversation.tenant_id == tenant.id,
            Conversation.whatsapp_connection_id == connection_id,
        )
        existing = None
        if phone:
            existing = q.filter(Conversation.contact_phone == phone).first()
        if not existing and remote_jid.endswith("@lid"):
            existing = q.filter(Conversation.contact_jid == remote_jid).first()

        if not existing:
            continue  # Nunca crear conversaciones desde chats.set

        import_evolution_chat_or_contact(
            db,
            tenant=tenant,
            record=record,
            whatsapp_connection_id=connection_id,
            instance_name=session.instance_name,
        )
        updated += 1

    if updated:
        db.commit()
    return updated


def _commit_and_publish_message(
    db,
    *,
    tenant: Tenant,
    message,
    event_type: str,
) -> None:
    """Confirma en BD y luego emite evento realtime (orden WhatsApp Web)."""
    from app.domain.entities import Conversation
    from app.application.realtime.realtime_service import (
        publish_conversation_updated,
        publish_message_event,
    )

    conv = (
        db.query(Conversation)
        .filter(Conversation.id == message.conversation_id)
        .first()
    )
    db.commit()
    if conv is not None:
        publish_message_event(tenant, conv, message, event_type=event_type)
        publish_conversation_updated(tenant.id, conv)


def _payload_instance(payload: dict) -> str:
    return str(
        payload.get("instance")
        or payload.get("instanceName")
        or (payload.get("data") or {}).get("instance")
        or ""
    )


def _owner_names(session: WhatsAppSession) -> set[str]:
    from app.application.whatsapp.whatsapp_status import build_owner_display_names

    return build_owner_display_names(session)


def _import_contacts_batch(
    db,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    records: list,
) -> int:
    """Procesa contacts.set/upsert: cachea nombres Y promueve conversaciones existentes.

    NO crea conversaciones nuevas — un contacto de agenda sin mensajes no debe aparecer
    en el panel. Pero sí actualiza:
    1. Conversaciones con teléfono lid:xxx → teléfono real cuando aprendemos el mapeo.
    2. Conversaciones con nombre placeholder → nombre real del contacto.
    """
    from app.application.sync.contact_name_cache_service import (
        apply_cached_names_to_conversations,
        remember_from_record,
    )
    from app.domain.entities import Conversation
    from app.shared.core.phone import (
        is_placeholder_contact_name,
        is_valid_whatsapp_phone,
        normalize_phone,
        resolve_contact_phone,
    )

    connection_id = session.active_connection_id
    if connection_id is None:
        return 0

    owner_names = _owner_names(session)

    # (lid_jid, real_phone, whatsapp_name) — whatsapp_name comes only from WhatsApp
    # profile fields (pushName/verifiedName/notify), NEVER from the phone book "name"
    # field. Phone book names are cached in Redis for potential future use but must NOT
    # be written directly to conversations — they can be wrong (e.g. user saved an old
    # number under a different person's name in their contacts).
    promotions: list[tuple[str, str, str]] = []

    for record in records:
        if not isinstance(record, dict):
            continue
        remember_from_record(session.instance_name, record, owner_names=owner_names)

        remote_jid = str(record.get("remoteJid") or record.get("id") or "")
        if not remote_jid:
            continue

        lid_jid = ""
        phone_jid = remote_jid
        if remote_jid.endswith("@lid"):
            lid_jid = remote_jid
            alt = str(record.get("jid") or record.get("phoneJid") or "")
            if alt and not alt.endswith("@lid"):
                phone_jid = alt
        elif record.get("lid"):
            lid_jid = str(record["lid"])

        phone = resolve_contact_phone(phone_jid) or ""

        # Only WhatsApp profile names — never phone book "name" field.
        # "name" = what the user typed in their phone contacts (unreliable for identity).
        # "pushName" / "verifiedName" / "notify" = what WhatsApp itself reports.
        whatsapp_name = str(
            record.get("pushName")
            or record.get("verifiedName")
            or record.get("notify")
            or ""
        ).strip()[:200]
        if owner_names:
            from app.shared.core.phone import is_owner_display_name
            if is_owner_display_name(whatsapp_name, owner_names):
                whatsapp_name = ""

        if lid_jid or phone:
            promotions.append((lid_jid, phone, whatsapp_name))

    if not promotions:
        return 0

    updated = 0

    for lid_jid, phone, whatsapp_name in promotions:
        real_phone = normalize_phone(phone) if is_valid_whatsapp_phone(phone) else ""

        # Promote lid: placeholder conversations to real phone.
        # Safety: skip if another conversation already uses that phone (avoid conflicts).
        if lid_jid and real_phone:
            conflict = (
                db.query(Conversation)
                .filter(
                    Conversation.tenant_id == tenant.id,
                    Conversation.whatsapp_connection_id == connection_id,
                    Conversation.contact_phone == real_phone,
                    Conversation.contact_jid != lid_jid,
                )
                .first()
            )
            if not conflict:
                n = (
                    db.query(Conversation)
                    .filter(
                        Conversation.tenant_id == tenant.id,
                        Conversation.whatsapp_connection_id == connection_id,
                        Conversation.contact_jid == lid_jid,
                        Conversation.contact_phone.like("lid:%"),
                    )
                    .update({"contact_phone": real_phone}, synchronize_session="fetch")
                )
                updated += n

        # Apply WhatsApp name to placeholder conversations.
        # Only uses pushName/verifiedName/notify — never phone book names.
        if whatsapp_name:
            q = db.query(Conversation).filter(
                Conversation.tenant_id == tenant.id,
                Conversation.whatsapp_connection_id == connection_id,
            )
            candidates: list[Conversation] = []
            if lid_jid:
                candidates += q.filter(Conversation.contact_jid == lid_jid).all()
            if real_phone:
                candidates += q.filter(Conversation.contact_phone == real_phone).all()
            seen_ids: set = set()
            for conv in candidates:
                if conv.id in seen_ids:
                    continue
                seen_ids.add(conv.id)
                if is_placeholder_contact_name(conv.contact_name, conv.contact_phone):
                    conv.contact_name = whatsapp_name
                    updated += 1

    if updated:
        try:
            db.commit()
        except Exception as exc:
            log.warning("_import_contacts_batch commit falló: %s", exc)
            db.rollback()

    return updated


def _apply_names_from_message_history(
    db,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    data,
) -> int:
    from app.application.sync.contact_identity_service import (
        ContactNamesLookup,
        apply_names_lookup_to_conversations,
    )
    from app.application.sync.contact_name_cache_service import (
        apply_cached_names_to_conversations,
        ingest_message_batch,
        load_cached_names,
    )
    from app.domain.entities import Conversation
    from app.shared.core.phone import is_placeholder_contact_name

    owner_names = _owner_names(session)
    ingest_message_batch(
        session.instance_name, data, owner_names=owner_names
    )
    if session.active_connection_id is None:
        return 0
    placeholders = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant.id,
            Conversation.whatsapp_connection_id == session.active_connection_id,
        )
        .all()
    )
    placeholders = [
        c
        for c in placeholders
        if is_placeholder_contact_name(c.contact_name, c.contact_phone)
    ]
    if not placeholders:
        return 0
    fixed = apply_cached_names_to_conversations(
        placeholders, session.instance_name, owner_names=owner_names
    )
    jid_names, phone_names = load_cached_names(session.instance_name)
    if jid_names or phone_names:
        lookup = ContactNamesLookup(
            jid_names=jid_names,
            phone_names=phone_names,
            lid_to_phone={},
            phone_to_lid={},
        )
        fixed += apply_names_lookup_to_conversations(
            placeholders, lookup, owner_names=owner_names
        )
    if fixed:
        db.commit()
        from app.application.realtime.realtime_service import publish_panel_event

        try:
            publish_panel_event(
                tenant.id,
                {"type": "contacts.enriched", "names_fixed": fixed, "status": "completed"},
            )
        except Exception:
            pass
    return fixed


def process_evolution_webhook(tenant_id: uuid.UUID, payload: dict) -> None:
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

        from app.domain.entities.enums import WhatsAppStatus

        _CONNECT_EVENTS = {"connection.update", "qrcode.updated"}
        if (
            event not in _CONNECT_EVENTS
            and event != "messages.upsert"
            and tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value
        ):
            log.debug("Webhook ignorado — WhatsApp desconectado tenant=%s event=%s", tenant_id, event)
            return

        if event != "messages.upsert":
            dedup_id = build_dedup_id(payload)
            if is_duplicate_webhook(tenant_id, dedup_id):
                log.debug("Webhook duplicado ignorado tenant=%s id=%s", tenant_id, dedup_id)
                return

        from app.config import settings as app_settings

        if app_settings.chatwoot_enabled and event in {
            "messages.upsert",
            "messages.set",
            "chats.set",
            "chats.upsert",
            "chats.update",
            "contacts.set",
            "contacts.upsert",
            "contacts.update",
        }:
            log.debug("Webhook Evolution omitido (Chatwoot activo) event=%s", event)
            return

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
                from app.application.sync.contact_name_cache_service import remember_contact_name

                owner_names = _owner_names(session)
                for item in parse_messages_upsert(data):
                    msg_id = item.get("message_id") or ""
                    push_name = str(item.get("push_name") or "").strip()
                    name_jid = item.get("lid_jid") or item.get("remote_jid") or ""
                    if push_name and name_jid and not item.get("from_me"):
                        remember_contact_name(
                            session.instance_name,
                            name_jid,
                            push_name,
                            owner_names=owner_names,
                        )
                    if msg_id:
                        msg_dedup = f"messages.upsert:{msg_id}"
                        if is_duplicate_webhook(tenant_id, msg_dedup):
                            log.debug(
                                "Mensaje duplicado ignorado tenant=%s id=%s",
                                tenant_id,
                                msg_id,
                            )
                            continue

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

                    try:
                        if item.get("from_me"):
                            msg = save_outbound_from_phone(
                                db,
                                tenant=tenant,
                                evolution_message_id=item["message_id"],
                                remote_jid=item["remote_jid"],
                                body=item["body"],
                                message_key=item.get("key") if isinstance(item.get("key"), dict) else None,
                                lid_jid=item.get("lid_jid") or "",
                                whatsapp_connection_id=connection_id,
                                instance_name=session.instance_name,
                                publish=False,
                            )
                            event_type = "message.out"
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
                                instance_name=session.instance_name,
                                publish=False,
                            )
                            event_type = "message.in"

                        if msg is not None:
                            _commit_and_publish_message(
                                db,
                                tenant=tenant,
                                message=msg,
                                event_type=event_type,
                            )
                            if event_type == "message.in":
                                pending_ai_jobs.append((tenant.id, msg.conversation_id, msg.id))
                    except Exception:
                        log.exception(
                            "Error guardando mensaje tenant=%s id=%s",
                            tenant_id,
                            msg_id,
                        )
                        db.rollback()
            if pending_ai_jobs:
                from app.application.ai.ai_queue_service import flush_pending_ai_replies

                flush_pending_ai_replies(pending_ai_jobs)
            return
        elif event in ("messages.set",):
            from app.application.sync.history_sync_tracker import record_messages_set_batch

            record_messages_set_batch(tenant_id, payload)
            # Solo cachear nombres de historial — NO disparar sync adicional
            # (schedule_whatsapp_sync desde aquí crea un loop: chats.set → sync → chats.set)
            _apply_names_from_message_history(
                db, tenant=tenant, session=session, data=data
            )
        elif event in ("chats.set", "chats.upsert"):
            records = data if isinstance(data, list) else [data]
            from app.application.sync.contact_name_cache_service import remember_from_record

            owner_names = _owner_names(session)
            for record in records:
                if isinstance(record, dict):
                    remember_from_record(
                        session.instance_name, record, owner_names=owner_names
                    )
            _import_chats_progressive(
                db,
                tenant=tenant,
                session=session,
                records=records,
            )
            # NO llamar schedule_whatsapp_sync aquí — causa loop:
            # chats.set → schedule_whatsapp_sync → fetchChats → chats.set → ...
        elif event in ("contacts.set",):
            records = data if isinstance(data, list) else [data]
            _import_contacts_batch(
                db, tenant=tenant, session=session, records=records
            )
            # Sin schedule_whatsapp_sync — solo cachear nombres
        elif event == "contacts.upsert":
            records = data if isinstance(data, list) else [data]
            _import_contacts_batch(
                db, tenant=tenant, session=session, records=records
            )
        elif event == "contacts.update":
            records = data if isinstance(data, list) else [data]
            _import_contacts_batch(
                db, tenant=tenant, session=session, records=records
            )
        elif event == "chats.update":
            # chats.update solo actualiza metadatos de chats existentes — no crear nuevos
            records = data if isinstance(data, list) else [data]
            from app.domain.entities import Conversation
            from app.shared.core.phone import resolve_contact_phone

            for record in records:
                if not isinstance(record, dict):
                    continue
                remote_jid = str(record.get("remoteJid") or record.get("id") or "")
                if not remote_jid or remote_jid.endswith("@g.us"):
                    continue
                phone = resolve_contact_phone(remote_jid)
                if not phone:
                    continue
                existing = (
                    db.query(Conversation)
                    .filter(
                        Conversation.tenant_id == tenant.id,
                        Conversation.whatsapp_connection_id == session.active_connection_id,
                        Conversation.contact_phone == phone,
                    )
                    .first()
                )
                if existing:  # Solo actualizar si YA existe — no crear nuevas
                    import_evolution_chat_or_contact(
                        db,
                        tenant=tenant,
                        record=record,
                        whatsapp_connection_id=session.active_connection_id,
                        instance_name=session.instance_name,
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

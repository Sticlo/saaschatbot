from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.shared.core.phone import (
    is_lid_placeholder,
    is_placeholder_contact_name,
    is_valid_whatsapp_phone,
    jid_to_phone,
    lid_jid_from_lid_phone,
    normalize_phone,
    phone_match_tail,
    phone_to_evolution_number,
    resolve_contact_phone,
)
from app.domain.entities import (
    Conversation,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageSource,
    MessageStatus,
    Tenant,
    WhatsAppSession,
    WhatsAppStatus,
)
from app.infrastructure.cache.redis_client import publish_event, tenant_cache_key
from app.application.sync.contact_identity_service import apply_identity_to_conversation
from app.application.realtime.realtime_service import publish_message_event
from app.application.whatsapp.whatsapp_status import apply_session_status, resolve_whatsapp_status

log = logging.getLogger(__name__)


def _find_existing_message(
    db: Session,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    evolution_message_id: str,
    body: str,
    created_at: Optional[datetime] = None,
) -> Optional[Message]:
    if evolution_message_id:
        existing = (
            db.query(Message)
            .filter(
                Message.tenant_id == tenant_id,
                Message.evolution_message_id == evolution_message_id,
            )
            .first()
        )
        if existing:
            return existing

    normalized = body.strip()
    if not normalized:
        return None

    query = db.query(Message).filter(
        Message.conversation_id == conversation_id,
        Message.body == normalized,
    )
    if created_at is not None:
        from datetime import timedelta

        window_start = created_at - timedelta(seconds=5)
        window_end = created_at + timedelta(seconds=5)
        query = query.filter(
            Message.created_at >= window_start,
            Message.created_at <= window_end,
        )
    return query.order_by(Message.created_at.asc()).first()


_MEDIA_BODY_TYPES = ("image", "video", "audio", "sticker", "document", "ptt")


def _detect_media_type_from_body(body: str) -> Optional[str]:
    """Devuelve el tipo de media si el body es un placeholder como [image], None si no."""
    b = (body or "").strip().lower()
    for mt in _MEDIA_BODY_TYPES:
        if b.startswith(f"[{mt}"):
            return mt
    return None


def _extract_message_body(message_obj: dict) -> str:
    if not isinstance(message_obj, dict):
        return ""
    for key in (
        "conversation",
        "extendedTextMessage",
        "imageMessage",
        "videoMessage",
        "audioMessage",
        "documentMessage",
        "stickerMessage",
        "contactMessage",
        "locationMessage",
        "reactionMessage",
        "pollCreationMessage",
        "pollUpdateMessage",
        "buttonsMessage",
        "listMessage",
        "templateMessage",
    ):
        value = message_obj.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            text = (
                value.get("text")
                or value.get("caption")
                or value.get("description")
                or value.get("title")
                or ""
            )
            if text:
                return str(text)
            if key == "reactionMessage":
                emoji = value.get("text")
                if emoji:
                    return str(emoji)
            if key == "contactMessage":
                vcard = value.get("displayName") or value.get("vcard")
                if vcard:
                    return f"[contacto: {vcard}]"
            if key == "locationMessage":
                return "[ubicación]"
            if key != "conversation":
                label = key.replace("Message", "").lower() or "archivo"
                return f"[{label}]"
    return ""


def find_conversation_for_contact(
    db: Session,
    *,
    tenant_id: UUID,
    whatsapp_connection_id: UUID,
    contact_phone: str = "",
    contact_jid: str = "",
    instance_name: str = "",
) -> Optional[Conversation]:
    """Busca conversación existente tolerando @lid, E.164 y prefijos distintos."""
    jids_to_try: list[str] = []
    if contact_jid:
        jids_to_try.append(contact_jid)
    if is_lid_placeholder(contact_phone):
        derived = lid_jid_from_lid_phone(contact_phone)
        if derived and derived not in jids_to_try:
            jids_to_try.append(derived)

    phones_to_try: list[str] = []
    if contact_phone:
        phones_to_try.append(contact_phone)
    if is_valid_whatsapp_phone(contact_phone):
        normalized = normalize_phone(contact_phone)
        if normalized and normalized not in phones_to_try:
            phones_to_try.append(normalized)

    lid_to_phone: dict[str, str] = {}
    phone_to_lid: dict[str, str] = {}

    if instance_name:
        from app.config import settings
        from app.infrastructure.evolution.evolution_store import (
            fetch_bidirectional_lid_mappings,
            fetch_lid_jid_for_phone,
        )

        if settings.evolution_database_url:
            evo_lid, evo_phone = fetch_bidirectional_lid_mappings(
                settings.evolution_database_url, instance_name
            )
            lid_to_phone.update(evo_lid)
            phone_to_lid.update(evo_phone)
            for phone in list(phones_to_try):
                if not is_valid_whatsapp_phone(phone):
                    continue
                norm = normalize_phone(phone)
                lid_jid = phone_to_lid.get(phone_to_evolution_number(norm)) or phone_to_lid.get(norm)
                if not lid_jid:
                    lid_jid = fetch_lid_jid_for_phone(
                        settings.evolution_database_url, instance_name, norm
                    ) or ""
                if lid_jid and lid_jid not in jids_to_try:
                    jids_to_try.append(lid_jid)

    # Mapeos ya aprendidos en conversaciones de la app (@lid + teléfono en el mismo chat).
    app_rows = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.whatsapp_connection_id == whatsapp_connection_id,
        )
        .all()
    )
    for conv in app_rows:
        jid = str(conv.contact_jid or "")
        phone = str(conv.contact_phone or "")
        if jid.endswith("@lid") and is_valid_whatsapp_phone(phone):
            norm = normalize_phone(phone)
            lid_to_phone.setdefault(jid, norm)
            phone_to_lid.setdefault(phone_to_evolution_number(norm), jid)
            phone_to_lid.setdefault(norm, jid)
        elif is_lid_placeholder(phone):
            derived = lid_jid_from_lid_phone(phone)
            if derived:
                lid_to_phone.setdefault(derived, "")

    for jid in list(jids_to_try):
        if jid.endswith("@lid"):
            mapped = lid_to_phone.get(jid)
            if mapped and is_valid_whatsapp_phone(mapped):
                norm = normalize_phone(mapped)
                if norm not in phones_to_try:
                    phones_to_try.append(norm)
    for phone in list(phones_to_try):
        if not is_valid_whatsapp_phone(phone):
            continue
        norm = normalize_phone(phone)
        digits = phone_to_evolution_number(norm)
        lid_jid = phone_to_lid.get(digits) or phone_to_lid.get(norm)
        if lid_jid and lid_jid not in jids_to_try:
            jids_to_try.append(lid_jid)

    for jid in jids_to_try:
        conversation = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant_id,
                Conversation.contact_jid == jid,
                Conversation.whatsapp_connection_id == whatsapp_connection_id,
            )
            .first()
        )
        if conversation:
            return conversation

    for phone in phones_to_try:
        conversation = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant_id,
                Conversation.contact_phone == phone,
                Conversation.whatsapp_connection_id == whatsapp_connection_id,
            )
            .first()
        )
        if conversation:
            return conversation

    tail_phone = ""
    for phone in phones_to_try:
        if is_valid_whatsapp_phone(phone):
            tail_phone = phone
            break

    if tail_phone or jids_to_try:
        for conversation in app_rows:
            if tail_phone and phone_match_tail(conversation.contact_phone, tail_phone):
                return conversation
            for jid in jids_to_try:
                if conversation.contact_jid == jid:
                    return conversation
                if is_lid_placeholder(conversation.contact_phone):
                    if lid_jid_from_lid_phone(conversation.contact_phone) == jid:
                        return conversation

    return None


def get_or_create_conversation(
    db: Session,
    *,
    tenant_id: UUID,
    contact_phone: str,
    contact_name: str = "",
    contact_jid: str = "",
    whatsapp_connection_id: Optional[UUID] = None,
    instance_name: str = "",
) -> Conversation:
    if whatsapp_connection_id is None:
        raise ValueError("whatsapp_connection_id requerido para conversaciones WA")

    if is_valid_whatsapp_phone(contact_phone):
        contact_phone = normalize_phone(contact_phone)

    conversation = find_conversation_for_contact(
        db,
        tenant_id=tenant_id,
        whatsapp_connection_id=whatsapp_connection_id,
        contact_phone=contact_phone,
        contact_jid=contact_jid,
        instance_name=instance_name,
    )
    if conversation:
        apply_identity_to_conversation(
            conversation,
            contact_phone=contact_phone,
            contact_name=contact_name,
            contact_jid=contact_jid,
        )
        if contact_name and is_placeholder_contact_name(
            conversation.contact_name, conversation.contact_phone
        ):
            conversation.contact_name = contact_name
        return conversation

    conversation = Conversation(
        tenant_id=tenant_id,
        contact_phone=contact_phone,
        contact_name=contact_name or (contact_phone if is_valid_whatsapp_phone(contact_phone) else "Contacto"),
        contact_jid=contact_jid or None,
        whatsapp_connection_id=whatsapp_connection_id,
    )
    db.add(conversation)
    db.flush()
    return conversation


def _save_message(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    direction: str,
    source: str,
    body: str,
    status: str,
    evolution_message_id: str,
    increment_unread: bool,
    created_at: Optional[datetime] = None,
    publish: bool = True,
) -> Message:
    ts = created_at or datetime.now(timezone.utc)
    existing = _find_existing_message(
        db,
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        evolution_message_id=evolution_message_id,
        body=body,
        created_at=ts,
    )
    if existing:
        return existing

    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction=direction,
        source=source,
        body=body.strip(),
        status=status,
        evolution_message_id=evolution_message_id,
    )
    message.created_at = ts
    conversation.last_message_at = ts
    if increment_unread:
        conversation.unread_count = (conversation.unread_count or 0) + 1

    db.add(message)
    db.flush()

    if publish:
        event_type = "message.in" if direction == MessageDirection.IN.value else "message.out"
        publish_message_event(tenant, conversation, message, event_type=event_type)
    return message


def save_inbound_message(
    db: Session,
    *,
    tenant: Tenant,
    evolution_message_id: str,
    remote_jid: str,
    body: str,
    push_name: str = "",
    message_key: Optional[dict] = None,
    lid_jid: str = "",
    whatsapp_connection_id: Optional[UUID] = None,
    instance_name: str = "",
    publish: bool = True,
) -> Optional[Message]:
    if not body.strip() or whatsapp_connection_id is None:
        return None

    from app.application.conversations.contact_resolver_service import (
        resolve_canonical_conversation,
    )
    from app.application.whatsapp.whatsapp_status import build_owner_display_names

    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == tenant.id)
        .first()
    )
    owner_names = build_owner_display_names(session)
    inst = instance_name or (session.instance_name if session else "")

    conversation, phone, contact_jid = resolve_canonical_conversation(
        db,
        tenant_id=tenant.id,
        whatsapp_connection_id=whatsapp_connection_id,
        remote_jid=remote_jid,
        lid_jid=lid_jid,
        message_key=message_key,
        instance_name=inst,
        owner_names=owner_names,
    )

    if conversation is None:
        if not phone and not contact_jid:
            return None
        conversation = get_or_create_conversation(
            db,
            tenant_id=tenant.id,
            contact_phone=phone or f"lid:{contact_jid.split('@')[0]}",
            contact_name=push_name,
            contact_jid=contact_jid,
            whatsapp_connection_id=whatsapp_connection_id,
            instance_name=inst,
        )
    elif push_name and is_placeholder_contact_name(
        conversation.contact_name, conversation.contact_phone
    ):
        from app.shared.core.phone import is_owner_display_name

        if not owner_names or not is_owner_display_name(push_name, owner_names):
            conversation.contact_name = push_name

    if conversation.status == ConversationStatus.EXCLUDED.value:
        return None

    message = _save_message(
        db,
        tenant=tenant,
        conversation=conversation,
        direction=MessageDirection.IN.value,
        source=MessageSource.CONTACT.value,
        body=body,
        status=MessageStatus.RECEIVED.value,
        evolution_message_id=evolution_message_id,
        increment_unread=True,
        publish=publish,
    )

    if publish and conversation.bait_sent and not conversation.ai_active:
        conversation.ai_active = True
        from app.application.realtime.realtime_service import publish_conversation_updated

        publish_conversation_updated(tenant.id, conversation)

    return message


def _find_recent_outbound_echo(
    db: Session,
    *,
    tenant_id: UUID,
    whatsapp_connection_id: UUID,
    body: str,
    contact_phone: str,
    contact_jid: str = "",
    evolution_message_id: str = "",
    owner_names: Optional[set[str]] = None,
    window_seconds: int = 120,
) -> Optional[Conversation]:
    """Evita chat duplicado cuando el webhook llega sin evolution_message_id."""
    from datetime import timedelta

    normalized = body.strip()
    if not normalized:
        return None

    since = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    rows = (
        db.query(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(
            Message.tenant_id == tenant_id,
            Message.direction == MessageDirection.OUT.value,
            Message.body == normalized,
            Message.created_at >= since,
            Conversation.whatsapp_connection_id == whatsapp_connection_id,
        )
        .order_by(Message.created_at.desc())
        .limit(8)
        .all()
    )
    for row in rows:
        conv = row.conversation
        if contact_jid and conv.contact_jid == contact_jid:
            return conv
        if contact_phone and phone_match_tail(conv.contact_phone, contact_phone):
            return conv
        if contact_jid and is_lid_placeholder(conv.contact_phone):
            if lid_jid_from_lid_phone(conv.contact_phone) == contact_jid:
                return conv

    if not rows:
        return None

    seen: dict[UUID, Conversation] = {}
    for row in rows:
        seen[row.conversation_id] = row.conversation

    # Caso seguro: el eco no se pudo enlazar por jid/teléfono (típico de @lid sin mapeo),
    # pero hay exactamente UNA conversación con ese mismo texto saliente reciente. Es el
    # mismo chat que el panel/IA acaba de usar — reutilízalo en vez de duplicar.
    if len(seen) == 1:
        return next(iter(seen.values()))

    # Varias conversaciones con el mismo texto (p.ej. baits masivos): solo el heurístico
    # antiguo sin evolution_message_id intenta consolidar; con id es ambiguo, no arriesgar.
    if not evolution_message_id:
        from app.application.conversations.whatsapp_conversation_service import (
            pick_merge_primary,
        )

        primary = next(iter(seen.values()))
        for other in seen.values():
            primary, _ = pick_merge_primary(primary, other, owner_names=owner_names)
        return primary

    return None


def save_outbound_from_phone(
    db: Session,
    *,
    tenant: Tenant,
    evolution_message_id: str,
    remote_jid: str,
    body: str,
    message_key: Optional[dict] = None,
    lid_jid: str = "",
    whatsapp_connection_id: Optional[UUID] = None,
    instance_name: str = "",
    publish: bool = True,
) -> Optional[Message]:
    """Sincroniza mensajes enviados desde el celular (fromMe=true)."""
    if not body.strip() or whatsapp_connection_id is None:
        return None

    from app.application.conversations.contact_resolver_service import (
        resolve_canonical_conversation,
    )
    from app.application.whatsapp.whatsapp_status import build_owner_display_names

    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == tenant.id)
        .first()
    )
    owner_names = build_owner_display_names(session)
    inst = instance_name or (session.instance_name if session else "")

    # El panel ya guardó el mensaje — no duplicar en otro chat por JID distinto.
    if evolution_message_id:
        existing = (
            db.query(Message)
            .filter(
                Message.tenant_id == tenant.id,
                Message.evolution_message_id == evolution_message_id,
            )
            .first()
        )
        if existing:
            if publish:
                from app.application.realtime.realtime_service import (
                    publish_conversation_updated,
                    publish_message_event,
                )

                conv = (
                    db.query(Conversation)
                    .filter(Conversation.id == existing.conversation_id)
                    .first()
                )
                if conv is not None:
                    publish_message_event(
                        tenant, conv, existing, event_type="message.out"
                    )
                    publish_conversation_updated(tenant.id, conv)
            return existing

    conversation, phone, contact_jid = resolve_canonical_conversation(
        db,
        tenant_id=tenant.id,
        whatsapp_connection_id=whatsapp_connection_id,
        remote_jid=remote_jid,
        lid_jid=lid_jid,
        message_key=message_key,
        instance_name=inst,
        owner_names=owner_names,
    )

    if conversation is None:
        conversation = _find_recent_outbound_echo(
            db,
            tenant_id=tenant.id,
            whatsapp_connection_id=whatsapp_connection_id,
            body=body,
            contact_phone=phone,
            contact_jid=contact_jid,
            evolution_message_id=evolution_message_id,
            owner_names=owner_names,
        )

    if conversation is None:
        conversation = get_or_create_conversation(
            db,
            tenant_id=tenant.id,
            contact_phone=phone or f"lid:{contact_jid.split('@')[0]}",
            contact_jid=contact_jid,
            whatsapp_connection_id=whatsapp_connection_id,
            instance_name=inst,
        )
    else:
        apply_identity_to_conversation(
            conversation,
            contact_phone=phone,
            contact_name="",
            contact_jid=contact_jid,
        )
        if contact_jid.endswith("@lid") and is_valid_whatsapp_phone(phone):
            from app.application.conversations.contact_resolver_service import (
                record_contact_link,
            )

            record_contact_link(
                db,
                tenant_id=tenant.id,
                whatsapp_connection_id=whatsapp_connection_id,
                lid_jid=contact_jid,
                phone_e164=phone,
            )

    message = _save_message(
        db,
        tenant=tenant,
        conversation=conversation,
        direction=MessageDirection.OUT.value,
        source=MessageSource.AGENT.value,
        body=body,
        status=MessageStatus.SENT.value,
        evolution_message_id=evolution_message_id,
        increment_unread=False,
        publish=False,
    )

    if publish:
        db.refresh(message)
        conv = (
            db.query(Conversation)
            .filter(Conversation.id == message.conversation_id)
            .first()
        )
        if conv is not None:
            from app.application.realtime.realtime_service import (
                publish_conversation_updated,
                publish_message_event,
            )

            publish_message_event(tenant, conv, message, event_type="message.out")
            publish_conversation_updated(tenant.id, conv)

    return message


def import_evolution_chat_or_contact(
    db: Session,
    *,
    tenant: Tenant,
    record: dict,
    whatsapp_connection_id: Optional[UUID] = None,
) -> Optional[Conversation]:
    if whatsapp_connection_id is None:
        return None

    # contacts.set/upsert usa "id"; chats.set usa "remoteJid"
    remote_jid = str(record.get("remoteJid") or record.get("id") or "")
    if not remote_jid or remote_jid.endswith("@g.us"):
        return None

    # Para @lid, intentar resolver el JID de teléfono desde el campo "lid" o similar
    lid_jid = ""
    if remote_jid.endswith("@lid"):
        lid_jid = remote_jid
        # Buscar JID de teléfono alternativo
        alt = str(record.get("jid") or record.get("phoneJid") or "")
        if alt and not alt.endswith("@lid"):
            remote_jid = alt
    elif record.get("lid"):
        lid_jid = str(record["lid"])

    phone = resolve_contact_phone(remote_jid)
    if not phone and lid_jid:
        contact_jid = lid_jid
        phone = f"lid:{lid_jid.split('@')[0]}"
    else:
        contact_jid = lid_jid or (remote_jid if remote_jid.endswith("@lid") else "")

    if not phone:
        return None

    # "name" es el nombre de agenda del celular; "notify" y "pushName" son el nombre de perfil de WhatsApp
    name = (
        record.get("name")
        or record.get("notify")
        or record.get("pushName")
        or record.get("verifiedName")
        or ""
    )
    from app.domain.entities import WhatsAppSession
    from app.application.whatsapp.whatsapp_status import build_owner_display_names
    from app.shared.core.phone import is_owner_display_name

    wa_session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == tenant.id)
        .first()
    )
    owner_names = build_owner_display_names(wa_session)
    if owner_names and is_owner_display_name(str(name), owner_names):
        name = ""
    archived_raw = record.get("archived")
    conversation = get_or_create_conversation(
        db,
        tenant_id=tenant.id,
        contact_phone=phone,
        contact_name=str(name),
        contact_jid=contact_jid,
        whatsapp_connection_id=whatsapp_connection_id,
    )
    apply_identity_to_conversation(
        conversation,
        contact_phone=phone,
        contact_name=str(name),
        contact_jid=contact_jid,
        is_archived=bool(archived_raw) if archived_raw is not None else None,
    )
    from app.application.realtime.realtime_service import publish_conversation_updated

    publish_conversation_updated(tenant.id, conversation)
    return conversation


def import_messages_batch(
    db: Session,
    *,
    tenant: Tenant,
    data: Any,
    whatsapp_connection_id: Optional[UUID] = None,
) -> int:
    if whatsapp_connection_id is None:
        return 0
    imported = 0
    for item in parse_messages_upsert(data):
        if item.get("from_me"):
            msg = save_outbound_from_phone(
                db,
                tenant=tenant,
                evolution_message_id=item["message_id"],
                remote_jid=item["remote_jid"],
                body=item["body"],
                message_key=item.get("key") if isinstance(item.get("key"), dict) else None,
                lid_jid=item.get("lid_jid") or "",
                whatsapp_connection_id=whatsapp_connection_id,
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
                whatsapp_connection_id=whatsapp_connection_id,
            )
        if msg:
            imported += 1
    return imported


def parse_messages_upsert(data: Any) -> list[dict]:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("messages") or [data]
    else:
        return []

    parsed = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = item.get("key") or {}
        remote_jid = key.get("remoteJid") or ""
        remote_alt = key.get("remoteJidAlt") or ""
        lid_jid = ""
        phone_jid = ""
        if remote_jid.endswith("@lid"):
            lid_jid = remote_jid
            if remote_alt.endswith("@s.whatsapp.net"):
                phone_jid = remote_alt
        elif remote_jid.endswith("@s.whatsapp.net"):
            phone_jid = remote_jid
            if remote_alt.endswith("@lid"):
                lid_jid = remote_alt
        elif remote_alt.endswith("@lid"):
            lid_jid = remote_alt
            if remote_jid.endswith("@s.whatsapp.net"):
                phone_jid = remote_jid
        else:
            phone_jid = remote_jid or remote_alt
            lid_jid = remote_jid if remote_jid.endswith("@lid") else ""
        msg_id = key.get("id")
        body = _extract_message_body(item.get("message") or {})
        push_name = item.get("pushName") or ""
        from_me = bool(key.get("fromMe"))
        b64_data = item.get("base64") or ""
        msg_obj = item.get("message") or {}
        # Detectar mimetype desde el objeto de mensaje
        mimetype = ""
        for mtype in ("imageMessage", "videoMessage", "audioMessage", "stickerMessage", "documentMessage"):
            mdata = msg_obj.get(mtype)
            if isinstance(mdata, dict) and mdata.get("mimetype"):
                mimetype = mdata["mimetype"]
                break
        if phone_jid and msg_id:
            parsed.append(
                {
                    "remote_jid": phone_jid,
                    "lid_jid": lid_jid,
                    "message_id": str(msg_id),
                    "body": body or "[mensaje]",
                    "push_name": push_name,
                    "from_me": from_me,
                    "key": key,
                    "base64": b64_data,
                    "mimetype": mimetype,
                }
            )
    return parsed


def handle_connection_update(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    data: dict,
) -> None:
    from app.application.conversations.whatsapp_conversation_service import (
        needs_new_whatsapp_binding,
        notify_conversations_cleared,
        purge_whatsapp_conversations,
        start_new_whatsapp_binding,
    )

    previous_status = session.status
    state = data.get("state") or data.get("status") or data.get("connection")
    if isinstance(state, dict):
        state = state.get("state")
    mapped = resolve_whatsapp_status(str(state or ""), data)
    owner = data.get("owner") or data.get("wuid")

    if needs_new_whatsapp_binding(
        session,
        previous_status=previous_status,
        mapped_status=mapped,
        owner_jid=str(owner) if owner else None,
    ):
        start_new_whatsapp_binding(
            db,
            tenant=tenant,
            session=session,
            instance_name=session.instance_name,
            owner_jid=str(owner) if owner else None,
        )
    else:
        from app.application.conversations.whatsapp_conversation_service import ensure_whatsapp_binding_ready

        ensure_whatsapp_binding_ready(
            db,
            tenant=tenant,
            session=session,
            owner_jid=str(owner) if owner else None,
        )

    apply_session_status(
        session,
        tenant,
        mapped,
        owner_jid=str(owner) if owner else None,
    )

    if (
        mapped in {
            WhatsAppStatus.DISCONNECTED.value,
            WhatsAppStatus.BANNED.value,
            WhatsAppStatus.RESTRICTED.value,
        }
        and previous_status == WhatsAppStatus.CONNECTED.value
    ):
        from app.config import settings
        from app.infrastructure.evolution.evolution_store import purge_instance_stored_data
        from app.application.conversations.whatsapp_conversation_service import (
            notify_conversations_cleared,
            purge_all_tenant_whatsapp_conversations,
        )

        purge_all_tenant_whatsapp_conversations(db, tenant_id=tenant.id)
        if settings.evolution_database_url:
            purge_instance_stored_data(settings.evolution_database_url, session.instance_name)
        session.active_connection_id = None
        session.connection_started_at = None
        notify_conversations_cleared(tenant.id)

    from app.application.realtime.realtime_service import publish_whatsapp_status

    publish_whatsapp_status(
        tenant.id,
        status=mapped,
        qr_base64=session.qr_base64,
        phone_number=session.phone_number,
    )
    if mapped == WhatsAppStatus.CONNECTED.value:
        try:
            from app.infrastructure.evolution.evolution_client import evolution_client

            evolution_client.set_settings(
                session.instance_name,
                {
                    "rejectCall": False,
                    "groupsIgnore": True,
                    "alwaysOnline": False,
                    "readMessages": False,
                    "readStatus": False,
                    "syncFullHistory": True,
                },
                timeout=5.0,
            )
        except Exception:
            log.debug("syncFullHistory al conectar omitido", exc_info=True)
    db.flush()


def handle_qrcode_update(db: Session, session: WhatsAppSession, tenant: Tenant, data: dict) -> None:
    qr = data.get("qrcode") if isinstance(data.get("qrcode"), dict) else data
    base64 = None
    if isinstance(qr, dict):
        base64 = qr.get("base64") or qr.get("code")
    elif isinstance(qr, str):
        base64 = qr
    if base64:
        session.qr_base64 = base64
        session.qr_updated_at = datetime.now(timezone.utc)
        session.status = WhatsAppStatus.CONNECTING.value
        tenant.whatsapp_status = WhatsAppStatus.CONNECTING.value
        from app.application.realtime.realtime_service import publish_whatsapp_status

        publish_whatsapp_status(
            tenant.id,
            status=WhatsAppStatus.CONNECTING.value,
            qr_base64=base64,
            phone_number=session.phone_number,
        )
        db.flush()


def update_message_status(db: Session, tenant_id: UUID, data: dict) -> None:
    key = data.get("key") if isinstance(data, dict) else {}
    if not isinstance(key, dict):
        return
    msg_id = key.get("id")
    status = data.get("status") or data.get("update") or data.get("ack")
    if not msg_id:
        return

    message = (
        db.query(Message)
        .filter(Message.tenant_id == tenant_id, Message.evolution_message_id == str(msg_id))
        .first()
    )
    if not message:
        return

    status_map = {
        "SERVER_ACK": MessageStatus.SENT.value,
        "DELIVERY_ACK": MessageStatus.DELIVERED.value,
        "READ": MessageStatus.READ.value,
        "READ_ACK": MessageStatus.READ.value,
        "2": MessageStatus.DELIVERED.value,
        "3": MessageStatus.READ.value,
    }
    mapped = status_map.get(str(status).upper(), message.status)
    message.status = mapped
    db.flush()

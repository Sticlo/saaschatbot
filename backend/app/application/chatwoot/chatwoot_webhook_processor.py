from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.application.chatwoot.chatwoot_service import resolve_tenant_by_inbox_id
from app.application.messaging.message_service import (
    get_or_create_conversation,
    save_inbound_message,
    save_outbound_from_phone,
)
from app.application.messaging.webhook_processor import _commit_and_publish_message
from app.application.realtime.realtime_service import publish_conversation_updated
from app.config import settings
from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
from app.shared.core.phone import is_group_or_broadcast_jid, normalize_phone

log = logging.getLogger(__name__)


def _extract_phone(payload: dict) -> str:
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    meta = conversation.get("meta") if isinstance(conversation.get("meta"), dict) else {}
    sender_meta = meta.get("sender") if isinstance(meta.get("sender"), dict) else {}

    for raw in (
        sender.get("phone_number"),
        sender_meta.get("phone_number"),
        sender.get("identifier"),
        sender_meta.get("identifier"),
        conversation.get("source_id"),
    ):
        if raw:
            phone = normalize_phone(str(raw))
            if phone:
                return phone
    return ""


def _extract_name(payload: dict) -> str:
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    meta = conversation.get("meta") if isinstance(conversation.get("meta"), dict) else {}
    sender_meta = meta.get("sender") if isinstance(meta.get("sender"), dict) else {}
    return str(
        sender.get("name")
        or sender_meta.get("name")
        or conversation.get("contact_name")
        or ""
    ).strip()[:200]


def _parse_timestamp(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        text = str(raw).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except (TypeError, ValueError, OSError):
        return None


def _find_conversation(
    db: Session,
    *,
    tenant_id,
    connection_id,
    chatwoot_conversation_id: Optional[int],
    phone: str,
) -> Optional[Conversation]:
    if chatwoot_conversation_id:
        found = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant_id,
                Conversation.chatwoot_conversation_id == chatwoot_conversation_id,
            )
            .first()
        )
        if found:
            return found
    if phone:
        return (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant_id,
                Conversation.whatsapp_connection_id == connection_id,
                Conversation.contact_phone == phone,
            )
            .first()
        )
    return None


def process_chatwoot_webhook(db: Session, payload: dict) -> None:
    if not settings.chatwoot_enabled:
        return

    event = str(payload.get("event") or "").strip()
    if event not in {
        "message_created",
        "message_updated",
        "conversation_created",
        "conversation_updated",
    }:
        return

    conversation_data = payload.get("conversation")
    if not isinstance(conversation_data, dict) and event.startswith("conversation_"):
        conversation_data = payload
    if not isinstance(conversation_data, dict):
        conversation_data = {}

    inbox_id_raw = (
        conversation_data.get("inbox_id")
        or (payload.get("inbox") or {}).get("id")
        if isinstance(payload.get("inbox"), dict)
        else None
    )
    try:
        inbox_id = int(inbox_id_raw)
    except (TypeError, ValueError):
        log.debug("Chatwoot webhook sin inbox_id event=%s", event)
        return

    resolved = resolve_tenant_by_inbox_id(db, inbox_id)
    if resolved is None:
        log.debug("Chatwoot webhook inbox=%s sin tenant", inbox_id)
        return
    tenant, session = resolved
    if session.active_connection_id is None:
        log.debug("Chatwoot webhook tenant=%s sin active_connection_id", tenant.id)
        return

    cw_conv_id: Optional[int] = None
    try:
        cw_conv_id = int(conversation_data.get("id") or payload.get("id"))
    except (TypeError, ValueError):
        cw_conv_id = None

    if event in {"conversation_created", "conversation_updated"}:
        phone = _extract_phone({"conversation": conversation_data, "sender": payload.get("sender")})
        if is_group_or_broadcast_jid(str(conversation_data.get("meta", {}).get("sender", {}).get("identifier") or "")):
            return
        if not phone and not cw_conv_id:
            return
        existing = _find_conversation(
            db,
            tenant_id=tenant.id,
            connection_id=session.active_connection_id,
            chatwoot_conversation_id=cw_conv_id,
            phone=phone,
        )
        if existing:
            if cw_conv_id and not existing.chatwoot_conversation_id:
                existing.chatwoot_conversation_id = cw_conv_id
            name = _extract_name({"conversation": conversation_data})
            if name and not existing.contact_name:
                existing.contact_name = name
            db.commit()
            publish_conversation_updated(tenant.id, existing)
            return
        if not phone:
            return
        conv = get_or_create_conversation(
            db,
            tenant_id=tenant.id,
            contact_phone=phone,
            contact_name=_extract_name({"conversation": conversation_data}) or phone,
            contact_jid="",
            whatsapp_connection_id=session.active_connection_id,
        )
        if cw_conv_id:
            conv.chatwoot_conversation_id = cw_conv_id
        db.commit()
        publish_conversation_updated(tenant.id, conv)
        return

    # message_created / message_updated
    try:
        cw_msg_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return

    if (
        db.query(Message)
        .filter(Message.tenant_id == tenant.id, Message.chatwoot_message_id == cw_msg_id)
        .first()
    ):
        return

    phone = _extract_phone(payload)
    if not phone:
        return

    conv = _find_conversation(
        db,
        tenant_id=tenant.id,
        connection_id=session.active_connection_id,
        chatwoot_conversation_id=cw_conv_id,
        phone=phone,
    )
    if conv is None:
        conv = get_or_create_conversation(
            db,
            tenant_id=tenant.id,
            contact_phone=phone,
            contact_name=_extract_name(payload) or phone,
            contact_jid="",
            whatsapp_connection_id=session.active_connection_id,
        )
        if cw_conv_id:
            conv.chatwoot_conversation_id = cw_conv_id

    body = str(payload.get("content") or "").strip() or "[mensaje]"
    message_type = str(payload.get("message_type") or "").lower()
    from_me = message_type in {"outgoing", "template"}

    if from_me:
        msg = save_outbound_from_phone(
            db,
            tenant=tenant,
            evolution_message_id=f"cw:{cw_msg_id}",
            remote_jid=f"{phone.lstrip('+')}@s.whatsapp.net",
            body=body,
            message_key=None,
            lid_jid="",
            whatsapp_connection_id=session.active_connection_id,
            instance_name=session.instance_name,
            publish=False,
        )
        event_type = "message.out"
    else:
        msg = save_inbound_message(
            db,
            tenant=tenant,
            evolution_message_id=f"cw:{cw_msg_id}",
            remote_jid=f"{phone.lstrip('+')}@s.whatsapp.net",
            body=body,
            push_name=_extract_name(payload),
            message_key=None,
            lid_jid="",
            whatsapp_connection_id=session.active_connection_id,
            instance_name=session.instance_name,
            publish=False,
        )
        event_type = "message.in"

    if msg is None:
        return

    msg.chatwoot_message_id = cw_msg_id
    ts = _parse_timestamp(payload.get("created_at"))
    if ts is not None:
        msg.created_at = ts
        conv.last_message_at = ts

    _commit_and_publish_message(
        db,
        tenant=tenant,
        message=msg,
        event_type=event_type,
    )

    if event_type == "message.in":
        from app.application.ai.ai_queue_service import flush_pending_ai_replies

        flush_pending_ai_replies([(tenant.id, msg.conversation_id, msg.id)])

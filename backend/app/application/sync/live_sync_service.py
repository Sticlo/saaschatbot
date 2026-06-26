from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
from app.shared.core.phone import (
    is_lid_placeholder,
    is_valid_whatsapp_phone,
    phone_to_evolution_number,
)
from app.application.sync.chat_sync_service import _import_messages, _load_message_records
from app.application.realtime.realtime_service import publish_conversation_updated

log = logging.getLogger(__name__)


def _jids_for_conversation(conversation: Conversation) -> list[str]:
    jids: list[str] = []
    seen: set[str] = set()

    def add(jid: str) -> None:
        jid = (jid or "").strip()
        if jid and jid not in seen:
            seen.add(jid)
            jids.append(jid)

    if conversation.contact_jid:
        add(conversation.contact_jid)
    if is_valid_whatsapp_phone(conversation.contact_phone):
        add(f"{phone_to_evolution_number(conversation.contact_phone)}@s.whatsapp.net")
    if is_lid_placeholder(conversation.contact_phone):
        lid = conversation.contact_phone[4:]
        if lid:
            add(f"{lid}@lid" if "@" not in lid else lid)

    return jids


def pull_live_conversation_messages(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    limit: int = 50,
) -> tuple[int, list[Message]]:
    """Trae mensajes recientes desde Evolution DB/API (no depende del webhook)."""
    if session.active_connection_id is None:
        return 0, []

    if conversation.whatsapp_connection_id != session.active_connection_id:
        return 0, []

    jids = _jids_for_conversation(conversation)
    if not jids:
        return 0, []

    imported = 0
    seen_ids: set[str] = set()
    for jid in jids:
        records = _load_message_records(session.instance_name, jid, limit=limit)
        if not records:
            continue
        imported += _import_messages(
            db,
            tenant=tenant,
            conversation=conversation,
            records=records,
            expected_phone=conversation.contact_phone,
            expected_jid=conversation.contact_jid or "",
            seen_evolution_ids=seen_ids,
        )

    if imported:
        db.flush()
        publish_conversation_updated(tenant.id, conversation)

    db.commit()

    messages = (
        db.query(Message)
        .filter(
            Message.tenant_id == tenant.id,
            Message.conversation_id == conversation.id,
        )
        .order_by(Message.created_at.asc())
        .all()
    )
    return imported, messages

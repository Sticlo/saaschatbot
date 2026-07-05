from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
from app.domain.entities.enums import MessageDirection
from app.shared.core.phone import (
    is_lid_placeholder,
    is_valid_whatsapp_phone,
    phone_to_evolution_number,
)
from app.application.sync.chat_sync_service import _import_messages, _load_message_records
from app.application.realtime.realtime_service import publish_conversation_updated

log = logging.getLogger(__name__)


def _finalize_inbound_ai(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
) -> bool:
    """Auto-activa IA si aplica y encola respuesta al último entrante pendiente."""
    scheduled = False
    try:
        db.refresh(conversation)
        if tenant.ai_global_enabled and not conversation.ai_active:
            latest_in = (
                db.query(Message)
                .filter(
                    Message.conversation_id == conversation.id,
                    Message.direction == MessageDirection.IN.value,
                )
                .order_by(Message.created_at.desc())
                .first()
            )
            if latest_in:
                from app.application.ai.ai_auto_enable_service import (
                    maybe_auto_enable_ai_for_inbound,
                )

                if maybe_auto_enable_ai_for_inbound(
                    tenant=tenant,
                    conversation=conversation,
                    body=latest_in.body or "",
                ):
                    db.flush()
                    publish_conversation_updated(tenant.id, conversation)

        from app.application.ai.ai_service import maybe_schedule_ai_for_conversation

        scheduled = maybe_schedule_ai_for_conversation(
            db,
            tenant_id=tenant.id,
            conversation_id=conversation.id,
        )
        if scheduled:
            log.info("IA encolada tras sync conv=%s", conversation.id)
    except Exception:
        log.exception("finalize inbound AI conv=%s", conversation.id)
    return scheduled


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
    try:
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

        _finalize_inbound_ai(db, tenant=tenant, conversation=conversation)
        db.commit()
    except Exception:
        log.exception(
            "pull_live falló conv=%s jids=%s — devolviendo mensajes de BD",
            conversation.id,
            jids,
        )
        db.rollback()

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

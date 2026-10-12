from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import exists
from sqlalchemy.orm import Query, Session, aliased

from app.application.ai.ai_classifier_service import classify_inbound_message
from app.application.conversations.interest_alert_service import is_alert_phone
from app.application.messaging.message_service import (
    _detect_media_type_from_body,
    is_reaction_only,
    media_caption,
)
from app.infrastructure.ai import gemini_client
from app.domain.entities import Conversation, Message, Tenant
from app.domain.entities.enums import ConversationMode, MessageDirection, MessageSource


def owner_messages(db: Session, conversation_id: uuid.UUID) -> Query:
    """Lo que el dueño escribió él mismo (panel o celular). El eco de un envío de la IA puede
    guardarse como del dueño: se descarta si la IA mandó ese mismo texto en el chat."""
    bot = aliased(Message)
    return db.query(Message).filter(
        Message.conversation_id == conversation_id,
        Message.direction == MessageDirection.OUT.value,
        Message.source == MessageSource.AGENT.value,
        ~exists().where(
            bot.conversation_id == Message.conversation_id,
            bot.source.in_((MessageSource.BOT.value, MessageSource.BAIT.value)),
            bot.body == Message.body,
        ),
    )


def owner_wrote_in_chat(db: Session, conversation_id: uuid.UUID) -> bool:
    return db.query(owner_messages(db, conversation_id).exists()).scalar()


def maybe_auto_enable_ai_for_inbound(
    *,
    tenant: Tenant,
    conversation: Conversation,
    body: str,
    db: Optional[Session] = None,
) -> bool:
    """
    Activa IA automáticamente sin tocar cada chat:
    - Carnada enviada → siempre al responder.
    - Contacto nuevo (no importado) → si el mensaje parece consulta de negocio.
    - Chat importado (amigos/agenda) → nunca, salvo carnada.
    - Chat donde el dueño ya escribe (familia, amigos, clientes que atiende él) → nunca.
    - Nunca si el agente ya decidió la IA de este chat o está en modo manual.
    - Nunca en el chat del número que recibe las alertas (el propio dueño).
    """
    if not tenant.ai_global_enabled or conversation.ai_active:
        return False
    if conversation.ai_set_by_agent or conversation.mode == ConversationMode.MANUAL.value:
        return False
    if is_alert_phone(tenant, conversation.contact_phone):
        return False
    if is_reaction_only(body):
        return False

    if conversation.bait_sent:
        conversation.ai_active = True
        return True

    if conversation.imported_legacy:
        return False

    if db is not None and conversation.id is not None and owner_wrote_in_chat(db, conversation.id):
        return False

    # Un contacto nuevo que manda audio o foto casi siempre está consultando; el texto llega al procesarlo.
    media_type = _detect_media_type_from_body(body)
    if media_type in ("audio", "ptt", "image") and gemini_client.is_configured():
        conversation.ai_active = True
        return True

    classification = classify_inbound_message(
        (media_caption(body) if media_type else body or "").strip(),
        business_name=tenant.business_name,
    )
    category = classification.get("category", "")
    if category in ("duda", "interesado"):
        conversation.ai_active = True
        return True
    return False

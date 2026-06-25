from __future__ import annotations

import json
import logging
import random
import time
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import Conversation, Exclusion, Message, Tenant, TenantProfile, WhatsAppSession
from app.domain.entities.enums import ConversationMode, ConversationStatus, MessageDirection, MessageSource
from app.application.ai.ai_classifier_service import classify_inbound_message
from app.application.ai.ai_conversation_service import generate_reply
from app.infrastructure.ai.deepseek_client import DeepSeekError, is_configured
from app.application.messaging.message_service import _detect_media_type_from_body
from app.application.outbound.outbound_dedup_service import mark_phone_excluded
from app.application.realtime.realtime_service import publish_conversation_updated
from app.application.whatsapp.whatsapp_service import send_text_message

log = logging.getLogger(__name__)

OPT_OUT_REPLY = (
    "Entendido, no te escribiremos más. Disculpa la molestia. "
    "Si en algún momento cambias de opinión, aquí estaremos."
)
NO_INTEREST_REPLY = (
    "Perfecto, gracias por avisar. Que tengas un excelente día. "
    "Si más adelante te interesa, con gusto te ayudamos."
)
FALLBACK_REPLY = (
    "¡Hola! Gracias por escribir. Cuéntame qué te gustaría saber y con gusto te ayudo."
)


def ai_block_reason(tenant: Tenant, conversation: Conversation) -> Optional[str]:
    if not tenant.ai_global_enabled:
        return "IA global apagada en el panel"
    if not conversation.ai_active:
        return "IA desactivada en este chat"
    if conversation.mode != ConversationMode.AUTO.value:
        return "Modo manual activo en este chat"
    if conversation.status == ConversationStatus.EXCLUDED.value:
        return "Contacto excluido"
    return None


def should_ai_respond(tenant: Tenant, conversation: Conversation) -> bool:
    return ai_block_reason(tenant, conversation) is None


def is_respondable_text(body: str) -> bool:
    text = (body or "").strip()
    if not text:
        return False
    if _detect_media_type_from_body(text):
        return False
    return True


def maybe_schedule_ai_for_conversation(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> bool:
    """Encola IA si el último mensaje es entrante y aún no hay respuesta bot."""
    from app.domain.entities import Message
    from app.domain.entities.enums import MessageSource
    from app.application.ai.ai_queue_service import enqueue_ai_reply_ids

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    conversation = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
        .first()
    )
    if not tenant or not conversation or not should_ai_respond(tenant, conversation):
        return False

    latest_in = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN.value,
        )
        .order_by(Message.created_at.desc())
        .first()
    )
    if latest_in is None or not is_respondable_text(latest_in.body):
        return False

    replied = (
        db.query(Message.id)
        .filter(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.OUT.value,
            Message.source == MessageSource.BOT.value,
            Message.created_at >= latest_in.created_at,
        )
        .first()
    )
    if replied is not None:
        return False

    enqueue_ai_reply_ids(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        message_id=latest_in.id,
    )
    return True


def register_opt_out(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    reason: str = "opt_out",
) -> None:
    phone = conversation.contact_phone
    if not phone or phone.startswith("lid:"):
        conversation.status = ConversationStatus.EXCLUDED.value
        conversation.ai_active = False
        return

    existing = (
        db.query(Exclusion)
        .filter(Exclusion.tenant_id == tenant.id, Exclusion.phone_e164 == phone)
        .first()
    )
    if existing is None:
        db.add(
            Exclusion(
                tenant_id=tenant.id,
                phone_e164=phone,
                reason=reason[:255],
            )
        )

    mark_phone_excluded(tenant.id, phone)
    conversation.status = ConversationStatus.EXCLUDED.value
    conversation.ai_active = False
    publish_conversation_updated(tenant.id, conversation)


def _is_latest_inbound(db: Session, conversation_id: uuid.UUID, message_id: uuid.UUID) -> bool:
    latest = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN.value,
        )
        .order_by(Message.created_at.desc())
        .first()
    )
    return latest is not None and latest.id == message_id


def _load_history(db: Session, conversation_id: uuid.UUID) -> list[Message]:
    return (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
        .all()
    )


def process_ai_reply(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
) -> bool:
    """Procesa un job de IA. Retorna True si terminó (respondió o decidió no responder)."""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    conversation = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
        .first()
    )
    message = (
        db.query(Message)
        .filter(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
            Message.tenant_id == tenant_id,
        )
        .first()
    )
    session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()

    if not tenant or not conversation or not message or not session:
        if not message:
            log.warning(
                "IA job sin mensaje conv=%s msg=%s (¿race o mensaje borrado?)",
                conversation_id,
                message_id,
            )
        return True

    if message.direction != MessageDirection.IN.value:
        return True

    if not _is_latest_inbound(db, conversation_id, message_id):
        log.debug("IA skip: hay mensaje más reciente conv=%s", conversation_id)
        return True

    if not should_ai_respond(tenant, conversation):
        reason = ai_block_reason(tenant, conversation) or "desconocido"
        log.info("IA skip conv=%s: %s", conversation_id, reason)
        return True

    if not is_respondable_text(message.body):
        log.info("IA skip conv=%s: mensaje no respondible (%r)", conversation_id, message.body[:80])
        return True

    delay = random.uniform(
        settings.ai_reply_delay_min_seconds,
        settings.ai_reply_delay_max_seconds,
    )
    time.sleep(delay)

    db.refresh(conversation)
    db.refresh(tenant)
    if not should_ai_respond(tenant, conversation):
        return True
    if not _is_latest_inbound(db, conversation_id, message_id):
        return True

    profile = (
        db.query(TenantProfile).filter(TenantProfile.tenant_id == tenant_id).first()
    )

    try:
        classification = classify_inbound_message(
            message.body,
            business_name=tenant.business_name,
        )
    except Exception:
        log.exception("Error clasificando mensaje conv=%s", conversation_id)
        return True

    category = classification.get("category", "duda")
    log.info(
        "IA classify tenant=%s conv=%s category=%s",
        tenant_id,
        conversation_id,
        category,
    )

    if category == "ruido":
        return True

    if category == "opt_out":
        register_opt_out(db, tenant=tenant, conversation=conversation)
        try:
            send_text_message(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                text=OPT_OUT_REPLY,
                source=MessageSource.BOT.value,
            )
        except Exception:
            log.exception("Error enviando opt_out conv=%s", conversation_id)
        return True

    if category == "no_interesado":
        conversation.ai_active = False
        publish_conversation_updated(tenant.id, conversation)
        try:
            send_text_message(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                text=NO_INTEREST_REPLY,
                source=MessageSource.BOT.value,
            )
        except Exception:
            log.exception("Error enviando no_interesado conv=%s", conversation_id)
        return True

    history = _load_history(db, conversation_id)
    reply = FALLBACK_REPLY
    used_fallback = False
    try:
        generated = generate_reply(
            tenant=tenant,
            profile=profile,
            contact_name=conversation.contact_name,
            history=history,
        )
        if generated.strip():
            reply = generated.strip()
    except DeepSeekError as exc:
        used_fallback = True
        log.warning("IA fallback conv=%s: %s", conversation_id, exc)
        from app.application.realtime.realtime_service import publish_panel_event

        publish_panel_event(
            tenant.id,
            {
                "type": "ai.error",
                "conversation_id": str(conversation_id),
                "error": str(exc)[:300],
            },
        )

    if not reply.strip():
        return True

    try:
        send_text_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            text=reply.strip(),
            source=MessageSource.BOT.value,
        )
        if used_fallback:
            log.info("IA respondió con fallback conv=%s", conversation_id)
    except Exception:
        log.exception("Error enviando respuesta IA conv=%s", conversation_id)
        return False

    return True

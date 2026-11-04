"""El cliente preguntó algo que la IA no sabía y quedó esperando «te confirmo». Cuando el dueño lo
responde en Preguntas pendientes, la IA retoma ese chat con la respuesta: el cliente no se enfría."""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.application.ai.ai_auto_enable_service import owner_messages
from app.application.ai.ai_conversation_service import SAFETY_RULES, build_system_prompt, date_context, format_history
from app.application.ai.ai_followup_service import _clean
from app.domain.entities import AiQuestion, Conversation, Message, Tenant
from app.domain.entities.enums import ConversationInterest, MessageDirection, MessageSource, WhatsAppStatus
from app.infrastructure.ai.ai_text_provider import chat_completion
from app.infrastructure.cache.redis_client import get_redis
from app.infrastructure.persistence.database import SessionLocal

log = logging.getLogger(__name__)

# WhatsApp solo deja escribirle a quien escribió en las últimas 24 horas.
REPLY_WINDOW = timedelta(hours=23)
HISTORY_TURNS = 10
SKIP = "NO"

RELAY_INSTRUCTION = """El cliente te preguntó algo que no sabías, le dijiste que se lo confirmabas y el dueño del negocio ya te respondió:
- Lo que preguntó: {topic}
- Respuesta del dueño: {answer}
Escríbele UN mensaje corto retomando su pregunta con esa respuesta tal cual, sin cambiar precios, opciones ni condiciones (ej. «Te confirmo: …»).
Si la respuesta dice en qué opción sí está lo que pidió, ofrécesela y termina con una pregunta corta que lo acerque a comprar.
Si en la conversación ya se le respondió esto, o dijo que no le interesa o que no le escriban, responde solo: {skip}
Responde únicamente con el texto del mensaje, sin comillas ni JSON."""


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def relay_target(db: Session, row: AiQuestion, now: Optional[datetime] = None) -> Optional[Conversation]:
    """El chat al que vale la pena escribirle, o None si ya no corresponde."""
    from app.application.ai.ai_service import should_ai_respond

    if row.conversation_id is None or not row.answer:
        return None
    conversation = db.get(Conversation, row.conversation_id)
    if conversation is None or conversation.tenant_id != row.tenant_id:
        return None
    if conversation.interest_status == ConversationInterest.NOT_INTERESTED.value:
        return None
    tenant = db.get(Tenant, row.tenant_id)
    if tenant is None or tenant.whatsapp_session is None or tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value:
        return None
    if not should_ai_respond(tenant, conversation):
        return None
    now = now or datetime.now(timezone.utc)
    last_in = (
        db.query(Message.created_at)
        .filter(Message.conversation_id == conversation.id, Message.direction == MessageDirection.IN.value)
        .order_by(Message.created_at.desc())
        .first()
    )
    if last_in is None or now - _as_utc(last_in[0]) > REPLY_WINDOW:
        return None
    asked_at = row.last_asked_at or row.created_at
    if asked_at and db.query(owner_messages(db, conversation.id).filter(Message.created_at >= asked_at).exists()).scalar():
        return None
    return conversation


def compose_relay(tenant: Tenant, conversation: Conversation, history: list[Message], row: AiQuestion) -> Optional[str]:
    system = build_system_prompt(tenant, tenant.profile)
    messages = [
        {"role": "system", "content": system + date_context() + SAFETY_RULES},
        *format_history(history, limit=HISTORY_TURNS),
        {"role": "system", "content": RELAY_INSTRUCTION.format(topic=row.topic, answer=row.answer, skip=SKIP)},
    ]
    return _clean(chat_completion(messages, temperature=0.4, max_tokens=220))


def relay_answer(question_id: uuid.UUID) -> bool:
    from app.application.whatsapp.whatsapp_service import send_text_message

    db = SessionLocal()
    try:
        row = db.get(AiQuestion, question_id)
        conversation = relay_target(db, row) if row is not None else None
        if conversation is None:
            return False
        if not get_redis().set(f"ai:answer_relay:{row.id}:{conversation.id}", "1", nx=True, ex=24 * 3600):
            return False
        tenant = db.get(Tenant, row.tenant_id)
        history = (
            db.query(Message)
            .filter(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.asc())
            .all()
        )
        text = compose_relay(tenant, conversation, history, row)
        if text is None:
            log.info("Respuesta del dueño no reenviada: el chat ya no lo necesita conv=%s", conversation.id)
            return False
        send_text_message(
            db,
            tenant=tenant,
            session=tenant.whatsapp_session,
            conversation=conversation,
            text=text,
            source=MessageSource.BOT.value,
        )
        db.commit()
        log.info("IA le confirmó al cliente lo que respondió el dueño conv=%s", conversation.id)
        return True
    except Exception:
        db.rollback()
        log.exception("No se pudo retomar el chat con la respuesta del dueño q=%s", question_id)
        return False
    finally:
        db.close()


def relay_answer_soon(question_id: uuid.UUID) -> None:
    threading.Thread(target=relay_answer, args=(question_id,), name="ai-answer-relay", daemon=True).start()

"""Las reglas que Meta le pone a la API oficial, aplicadas a todo lo que sale de Omitel aunque el número
esté conectado por WhatsApp Web: así el número se comporta como uno oficial y no da motivos para
restringirlo.

- Nunca se le escribe primero a alguien que no le ha escrito al negocio.
- Mensaje libre (IA, dueño desde el panel, atajos, seguimientos) solo dentro de las 24 horas desde
  el último mensaje del cliente.
- Pasadas las 24 horas solo salen avisos de algo que el cliente pidió (su cita, su pedido), que en la
  API oficial serían plantillas de utilidad."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import Conversation, Message
from app.domain.entities.enums import MessageDirection
from app.infrastructure.evolution.evolution_client import EvolutionAPIError

CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)

KIND_REPLY = "reply"
KIND_UTILITY = "utility"

NEVER_WROTE = (
    "Este contacto nunca le ha escrito al negocio. WhatsApp no permite escribirle primero: "
    "tiene que escribirte él."
)
WINDOW_CLOSED = (
    "Pasaron más de 24 horas desde el último mensaje del cliente. WhatsApp solo permite responder "
    "dentro de esas 24 horas: cuando te vuelva a escribir, podrás contestarle."
)


class MessagingPolicyError(EvolutionAPIError):
    def __init__(self, message: str):
        # 403: quien envía lo toma como definitivo y no reintenta.
        super().__init__(message, status_code=403)


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def last_customer_message_at(db: Session, conversation_id) -> Optional[datetime]:
    value = (
        db.query(func.max(Message.created_at))
        .filter(Message.conversation_id == conversation_id, Message.direction == MessageDirection.IN.value)
        .scalar()
    )
    return as_utc(value) if value is not None else None


def reply_window_closes_at(db: Session, conversation_id) -> Optional[datetime]:
    last_in = last_customer_message_at(db, conversation_id)
    return last_in + CUSTOMER_SERVICE_WINDOW if last_in is not None else None


def ensure_can_message(
    db: Session,
    conversation: Conversation,
    *,
    kind: str = KIND_REPLY,
    now: Optional[datetime] = None,
) -> None:
    if not settings.whatsapp_official_rules:
        return
    last_in = last_customer_message_at(db, conversation.id)
    if last_in is None:
        raise MessagingPolicyError(NEVER_WROTE)
    if kind == KIND_UTILITY:
        return
    now = now or datetime.now(timezone.utc)
    if now - last_in > CUSTOMER_SERVICE_WINDOW:
        raise MessagingPolicyError(WINDOW_CLOSED)

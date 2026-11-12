"""Las reglas que Meta le pone a la API oficial, aplicadas a todo lo que sale de Omitel aunque el número
esté conectado por WhatsApp Web: así el número se comporta como uno oficial y no da motivos para
restringirlo.

- Nunca se le escribe primero a alguien que no le ha escrito al negocio.
- Mensaje libre (IA, dueño desde el panel, atajos, seguimientos) solo dentro de las 24 horas desde
  el último mensaje del cliente.
- Pasadas las 24 horas solo salen avisos de algo que el cliente pidió (su cita, su pedido), que en la
  API oficial serían plantillas de utilidad.
- Los avisos al dueño y al equipo también salen del número del negocio: solo a quien ya le escribió.
- Con whatsapp_proactive_messages apagado nada sale por iniciativa propia: los avisos de utilidad
  también exigen las 24 horas y no salen avisos al dueño ni al equipo por WhatsApp."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_
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


TEAM_NEVER_WROTE = (
    "Ese número nunca le ha escrito al WhatsApp del negocio y escribirle primero puede hacer que "
    "bloqueen el número. Desde ese teléfono, mándale un «hola» al WhatsApp del negocio y vuelve a probar."
)

TEAM_ALERTS_OFF = (
    "Los avisos por WhatsApp están apagados para cuidar el número del negocio: escribirle a alguien "
    "que no acaba de escribir puede hacer que lo bloqueen. Los avisos te aparecen en el panel."
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


def _tail10(column):
    return func.right(func.regexp_replace(func.coalesce(column, ""), r"\D", "", "g"), 10)


def phone_has_written(db: Session, tenant_id, phone: str) -> bool:
    tail = re.sub(r"\D", "", phone or "")[-10:]
    if len(tail) < 10:
        return False
    return db.query(
        db.query(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(
            Conversation.tenant_id == tenant_id,
            Message.direction == MessageDirection.IN.value,
            or_(
                _tail10(Conversation.contact_phone) == tail,
                _tail10(func.split_part(Conversation.contact_jid, "@", 1)) == tail,
            ),
        )
        .exists()
    ).scalar()


def ensure_can_alert(tenant_id, phone: str) -> None:
    """Avisos al dueño/equipo: no tienen conversación propia, así que se busca por número."""
    if not settings.whatsapp_official_rules:
        return
    if not settings.whatsapp_proactive_messages:
        raise MessagingPolicyError(TEAM_ALERTS_OFF)
    from app.infrastructure.persistence.database import SessionLocal

    db = SessionLocal()
    try:
        if not phone_has_written(db, tenant_id, phone):
            raise MessagingPolicyError(TEAM_NEVER_WROTE)
    finally:
        db.close()


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
    if kind == KIND_UTILITY and settings.whatsapp_proactive_messages:
        return
    now = now or datetime.now(timezone.utc)
    if now - last_in > CUSTOMER_SERVICE_WINDOW:
        raise MessagingPolicyError(WINDOW_CLOSED)

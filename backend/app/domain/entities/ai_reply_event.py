from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, event, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.domain.entities.conversation import Conversation, Message
from app.domain.entities.enums import MessageDirection, MessageSource
from app.infrastructure.persistence.database import Base


class AiReplyEvent(Base):
    """Una respuesta que la IA envió por WhatsApp. Sin FK al chat a propósito: desvincular
    WhatsApp borra chats y mensajes, y los resultados del negocio no se pueden borrar con ellos."""

    __tablename__ = "ai_reply_events"
    __table_args__ = (Index("ix_ai_reply_events_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # Teléfono del cliente (o el id del chat si no hay): cuenta clientes distintos atendidos.
    contact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


@event.listens_for(Session, "before_flush")
def _record_ai_replies(session: Session, _flush_context, _instances) -> None:
    for obj in list(session.new):
        if not (
            isinstance(obj, Message)
            and obj.source == MessageSource.BOT.value
            and obj.direction == MessageDirection.OUT.value
        ):
            continue
        conversation = session.get(Conversation, obj.conversation_id) if obj.conversation_id else None
        key = (conversation.contact_phone if conversation else "") or str(obj.conversation_id or "")
        session.add(AiReplyEvent(tenant_id=obj.tenant_id, contact_key=key[:64]))

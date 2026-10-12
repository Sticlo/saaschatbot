from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.database import Base


class AiQuestion(Base):
    """Algo que un cliente preguntó y la IA no sabía. El dueño lo responde una vez y la IA lo aprende."""

    __tablename__ = "ai_questions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Qué dato faltó, en palabras de la IA: «costo del envío a Cali».
    topic: Mapped[str] = mapped_column(String(160), nullable=False)
    topic_key: Mapped[str] = mapped_column(String(160), nullable=False)
    # Lo que escribió el cliente, para que el dueño entienda el contexto.
    question: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # open | answered
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    times_asked: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_asked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    answered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

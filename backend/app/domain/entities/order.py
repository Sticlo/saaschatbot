from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.database import Base


class Order(Base):
    """Un pedido que cerró la IA. El dueño solo lo despacha. Guarda el contacto aparte porque
    desvincular WhatsApp borra los chats y el registro de lo vendido debe quedar."""

    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_tenant_status", "tenant_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    contact_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    contact_phone: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    items: Mapped[str] = mapped_column(Text, nullable=False)
    total: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    customer: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    delivery: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    payment: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    # El cliente mandó comprobante. Sin verificar: puede ser falso.
    payment_proof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # pending = esperando pago · ready = listo para despachar · dispatched · cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    tracking: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

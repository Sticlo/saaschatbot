from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.database import Base


class WhatsAppContactLink(Base):
    """Puente persistente @lid ↔ teléfono por sesión WA (no depende de Evolution)."""

    __tablename__ = "whatsapp_contact_links"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "whatsapp_connection_id",
            "lid_jid",
            name="uq_wa_contact_links_lid",
        ),
        UniqueConstraint(
            "tenant_id",
            "whatsapp_connection_id",
            "phone_e164",
            name="uq_wa_contact_links_phone",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    whatsapp_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    lid_jid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

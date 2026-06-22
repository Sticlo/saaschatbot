from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import WhatsAppStatus


class WhatsAppSession(Base):
    __tablename__ = "whatsapp_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    instance_name: Mapped[str] = mapped_column(
        String(80), unique=True, nullable=False, index=True
    )
    phone_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WhatsAppStatus.DISCONNECTED.value
    )
    qr_base64: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    qr_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_connected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_disconnected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active_connection_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    connection_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    bound_owner_jid: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="whatsapp_session")  # noqa: F821

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.database import Base

DEFAULT_WORK_DAYS = [0, 1, 2, 3, 4, 5]


class StaffMember(Base):
    """Quien atiende citas (barbero, estilista, profesional…)."""

    __tablename__ = "staff_members"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 0 = lunes … 6 = domingo
    work_days: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=lambda: list(DEFAULT_WORK_DAYS),
        server_default=text("'[0,1,2,3,4,5]'::jsonb"),
    )
    # Vacío = horario del negocio.
    start_time: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    end_time: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

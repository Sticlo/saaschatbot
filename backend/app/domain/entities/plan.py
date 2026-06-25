from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.persistence.database import Base

# UUID fijo del plan Pro — referencia estable en migraciones y seeds
DEFAULT_PLAN_ID = uuid.UUID("a0000000-0000-4000-8000-000000000001")


class Plan(Base):
    """Catálogo de planes. Hoy solo vendemos 'pro'; la tabla permite agregar más."""

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price_cop: Mapped[int] = mapped_column(Integer, nullable=False)
    price_usd_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    trial_bait_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    daily_bait_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    max_team_members: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    features: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )  # visible en pricing / checkout
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    subscriptions: Mapped[list["Subscription"]] = relationship(  # noqa: F821
        back_populates="plan"
    )

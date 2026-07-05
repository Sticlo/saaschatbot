from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.persistence.database import Base
from app.domain.entities.enums import TenantPlan, WhatsAppStatus


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    plan: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TenantPlan.TRIAL.value
    )
    ai_global_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    whatsapp_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WhatsAppStatus.DISCONNECTED.value
    )
    daily_bait_limit: Mapped[int] = mapped_column(nullable=False, default=10)
    trial_bait_used: Mapped[int] = mapped_column(nullable=False, default=0)
    disclaimer_accepted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    users: Mapped[list["User"]] = relationship(  # noqa: F821
        back_populates="tenant", cascade="all, delete-orphan"
    )
    profile: Mapped[Optional["TenantProfile"]] = relationship(  # noqa: F821
        back_populates="tenant", uselist=False, cascade="all, delete-orphan"
    )
    subscription: Mapped[Optional["Subscription"]] = relationship(  # noqa: F821
        back_populates="tenant", uselist=False, cascade="all, delete-orphan"
    )
    whatsapp_session: Mapped[Optional["WhatsAppSession"]] = relationship(  # noqa: F821
        back_populates="tenant", uselist=False, cascade="all, delete-orphan"
    )


class TenantProfile(Base):
    __tablename__ = "tenant_profiles"

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
    onboarding_answers: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    products_services: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_customer: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    price_range: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    location_hours: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    tone: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    restrictions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    maps_prospect_business: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    maps_prospect_city: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    ai_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="qualify")
    ai_system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bait_message_template: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quick_shortcuts: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    schedule_open_time: Mapped[str] = mapped_column(String(5), nullable=False, default="08:00")
    schedule_close_time: Mapped[str] = mapped_column(String(5), nullable=False, default="18:00")
    schedule_slot_minutes: Mapped[int] = mapped_column(nullable=False, default=60)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="profile")

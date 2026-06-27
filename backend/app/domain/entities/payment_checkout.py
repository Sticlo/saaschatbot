from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.persistence.database import Base


class PaymentCheckoutStatus:
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    ERROR = "error"


class PaymentCheckout(Base):
    __tablename__ = "payment_checkouts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reference: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    amount_in_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="COP")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PaymentCheckoutStatus.PENDING
    )
    customer_email: Mapped[str] = mapped_column(String(255), nullable=False)
    wompi_transaction_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    plan: Mapped["Plan"] = relationship()  # noqa: F821

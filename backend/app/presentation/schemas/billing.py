from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class BillingConfigResponse(BaseModel):
    enabled: bool
    public_key: Optional[str] = None
    sandbox: bool = False
    sync_enabled: bool = False


class CheckoutCreateRequest(BaseModel):
    plan_slug: str = Field(min_length=2, max_length=40)


class CheckoutCreateResponse(BaseModel):
    reference: str
    amount_in_cents: int
    currency: str
    public_key: str
    integrity_signature: str
    redirect_url: str
    plan_slug: str
    plan_name: str
    customer_email: str
    customer_name: str


class CheckoutStatusResponse(BaseModel):
    reference: str
    status: str
    plan_slug: Optional[str] = None
    plan_name: Optional[str] = None
    paid_at: Optional[datetime] = None
    wompi_transaction_id: Optional[str] = None


class CheckoutSyncRequest(BaseModel):
    transaction_id: str = Field(min_length=4, max_length=64)

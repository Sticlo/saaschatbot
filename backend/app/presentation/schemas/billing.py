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
    auto_renew_enabled: bool = False
    api_base: Optional[str] = None


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
    failure_reason: Optional[str] = None


class CheckoutSyncRequest(BaseModel):
    transaction_id: str = Field(min_length=4, max_length=64)


class WompiTermsResponse(BaseModel):
    acceptance_permalink: str
    personal_data_permalink: str


class PaymentMethodRequest(BaseModel):
    type: str = Field(pattern="^(CARD|NEQUI)$")
    token: str = Field(min_length=8, max_length=200)
    accept_auto_renew: bool = False
    accept_wompi_terms: bool = False
    plan_slug: Optional[str] = Field(default=None, max_length=40)
    brand: Optional[str] = Field(default=None, max_length=30)
    last_four: Optional[str] = Field(default=None, max_length=4)
    phone_last_four: Optional[str] = Field(default=None, max_length=4)


class PaymentMethodResponse(BaseModel):
    auto_renew: bool
    payment_method_label: Optional[str] = None
    charge: Optional[CheckoutStatusResponse] = None
    charge_error: Optional[str] = None


class CancelSubscriptionRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=40)
    feedback: str = Field(default="", max_length=500)

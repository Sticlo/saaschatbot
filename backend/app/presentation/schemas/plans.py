from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    description: Optional[str]
    price_cop: int
    price_usd_cents: Optional[int]
    trial_bait_limit: int
    daily_bait_limit: int
    max_team_members: int
    features: Optional[dict]
    is_active: bool
    is_public: bool
    sort_order: int


class SubscriptionSummaryResponse(BaseModel):
    status: str
    tenant_plan: str
    plan: PlanResponse
    is_trial: bool
    is_paid: bool
    trial_bait_limit: int
    trial_bait_used: int
    trial_bait_remaining: int
    daily_bait_limit: int
    can_send_outbound: bool
    needs_payment: bool
    current_period_start: Optional[datetime]
    current_period_end: Optional[datetime]


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: Optional[uuid.UUID]
    user_id: Optional[uuid.UUID]
    action: str
    details: Optional[dict]
    message: Optional[str]
    ip_address: Optional[str]
    created_at: datetime

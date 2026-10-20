from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    email: EmailStr


class SignupAttribution(BaseModel):
    """UTM, referido y lead (número del negocio) con los que llegó al sitio."""

    utm_source: Optional[str] = Field(default=None, max_length=200)
    utm_medium: Optional[str] = Field(default=None, max_length=200)
    utm_campaign: Optional[str] = Field(default=None, max_length=200)
    utm_content: Optional[str] = Field(default=None, max_length=200)
    utm_term: Optional[str] = Field(default=None, max_length=200)
    ref: Optional[str] = Field(default=None, max_length=200)
    lead: Optional[str] = Field(default=None, max_length=30)


class RegisterRequest(BaseModel):
    business_name: str = Field(min_length=2, max_length=255)
    owner_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    accept_legal: bool = False
    accept_marketing: bool = False
    attribution: Optional[SignupAttribution] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class EmailLookupRequest(BaseModel):
    email: EmailStr


class EmailLookupResponse(BaseModel):
    email: EmailStr
    exists: bool


class MagicLinkRequest(BaseModel):
    email: EmailStr
    business_name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    owner_name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    accept_legal: bool = False
    accept_marketing: bool = False
    attribution: Optional[SignupAttribution] = None


class MagicLinkResponse(BaseModel):
    sent: bool
    needs_signup: bool = False
    message: str = ""
    dev_link: Optional[str] = None


class MagicLinkVerifyRequest(BaseModel):
    token: str = Field(min_length=16, max_length=256)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    message: str
    dev_link: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=16, max_length=256)
    password: str = Field(min_length=8, max_length=128)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: EmailStr
    full_name: str
    role: str
    is_active: bool
    last_login_at: Optional[datetime]
    created_at: datetime


class TenantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_name: str
    slug: str
    plan: str
    ai_global_enabled: bool
    ai_autostart_pending: bool = False
    whatsapp_status: str
    daily_bait_limit: int
    trial_bait_used: int
    disclaimer_accepted_at: Optional[datetime]
    is_active: bool
    created_at: datetime


class TenantProfileUpdate(BaseModel):
    onboarding_answers: Optional[dict] = None
    ai_system_prompt: Optional[str] = None
    bait_message_template: Optional[str] = None


class TenantProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    onboarding_answers: Optional[dict]
    ai_system_prompt: Optional[str]
    bait_message_template: Optional[str]


class TenantSettingsUpdate(BaseModel):
    ai_global_enabled: Optional[bool] = None
    business_name: Optional[str] = Field(default=None, min_length=2, max_length=255)


class AlertRecipient(BaseModel):
    name: str = Field(default="", max_length=60)
    phone: str = Field(default="", max_length=32)
    scope: str = Field(default="all", pattern="^(all|sales)$")


class InterestAlertSettings(BaseModel):
    recipients: list[AlertRecipient] = Field(default_factory=list, max_length=10)
    alert_threshold: int = Field(default=10, ge=1, le=500)


class InterestAlertResponse(InterestAlertSettings):
    pending_count: int = 0
    warning: Optional[str] = None


class FollowupSettings(BaseModel):
    followup_hours: int = Field(ge=0, le=24)


class InviteUserRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(default="agent", pattern="^(agent|viewer)$")


class UpdateUserRoleRequest(BaseModel):
    role: str = Field(pattern="^(owner|agent|viewer)$")

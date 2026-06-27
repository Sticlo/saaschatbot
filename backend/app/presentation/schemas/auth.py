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


class RegisterRequest(BaseModel):
    business_name: str = Field(min_length=2, max_length=255)
    owner_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


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


class InviteUserRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(default="agent", pattern="^(agent|viewer)$")


class UpdateUserRoleRequest(BaseModel):
    role: str = Field(pattern="^(owner|agent|viewer)$")

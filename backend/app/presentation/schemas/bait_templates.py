from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class BaitButtonInput(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    value: str = Field(default="", max_length=120)


class BaitButtonResponse(BaseModel):
    type: str = "reply"
    label: str
    value: str


class BaitTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=2000)
    image_path: Optional[str] = Field(default=None, max_length=512)
    buttons: list[BaitButtonInput] = Field(default_factory=list, max_length=3)
    button_title: Optional[str] = Field(default=None, max_length=120)
    button_footer: Optional[str] = Field(default=None, max_length=255)
    is_default: bool = False


class BaitTemplateUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    text: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    image_path: Optional[str] = Field(default=None, max_length=512)
    clear_image: bool = False
    buttons: Optional[list[BaitButtonInput]] = None
    button_title: Optional[str] = Field(default=None, max_length=120)
    button_footer: Optional[str] = Field(default=None, max_length=255)
    is_default: Optional[bool] = None


class BaitTemplateResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    text: str
    image_path: Optional[str]
    image_url: Optional[str] = None
    buttons: Optional[list[BaitButtonResponse]]
    button_title: Optional[str]
    button_footer: Optional[str]
    is_default: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BaitTemplatePreviewRequest(BaseModel):
    contact_name: str = Field(default="María", max_length=120)


class BaitTemplatePreviewResponse(BaseModel):
    text: str
    image_path: Optional[str]
    image_url: Optional[str] = None
    buttons: list[BaitButtonResponse]
    button_title: Optional[str]
    button_footer: Optional[str]


class GenerateBaitTemplateRequest(BaseModel):
    tone: str = Field(default="", max_length=120)


class GenerateBaitTemplateResponse(BaseModel):
    text: str
    buttons: list[BaitButtonResponse]
    button_title: Optional[str]
    button_footer: Optional[str]


class AssetUploadResponse(BaseModel):
    image_path: str
    image_url: str


class BusinessProfileUpdate(BaseModel):
    industry: Optional[str] = Field(default=None, max_length=255)
    products_services: Optional[str] = Field(default=None, max_length=2000)
    target_customer: Optional[str] = Field(default=None, max_length=500)
    price_range: Optional[str] = Field(default=None, max_length=255)
    location_hours: Optional[str] = Field(default=None, max_length=500)
    tone: Optional[str] = Field(default=None, max_length=120)
    restrictions: Optional[str] = Field(default=None, max_length=1000)
    maps_prospect_business: Optional[str] = Field(default=None, max_length=2000)
    maps_prospect_city: Optional[str] = Field(default=None, max_length=120)
    ai_system_prompt: Optional[str] = Field(default=None, max_length=4000)
    bait_message_template: Optional[str] = Field(default=None, max_length=2000)


class BusinessProfileResponse(BaseModel):
    business_name: str
    industry: Optional[str] = None
    products_services: Optional[str] = None
    target_customer: Optional[str] = None
    price_range: Optional[str] = None
    location_hours: Optional[str] = None
    tone: Optional[str] = None
    restrictions: Optional[str] = None
    maps_prospect_business: Optional[str] = None
    maps_prospect_city: Optional[str] = None
    ai_system_prompt: Optional[str] = None
    bait_message_template: Optional[str] = None
    ai_summary: list[str] = Field(default_factory=list)

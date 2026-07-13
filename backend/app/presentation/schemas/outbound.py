from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class LeadInput(BaseModel):
    phone: str
    name: str = ""


class ImportLeadsRequest(BaseModel):
    leads: list[LeadInput] = Field(..., min_length=1, max_length=500)
    source: str = "manual"


class ImportLeadsResponse(BaseModel):
    added: int
    skipped: int
    invalid: int


class WhatsAppContactResponse(BaseModel):
    phone_e164: str
    name: str
    source: str
    verified: bool = True


class WhatsAppContactDirectoryResponse(BaseModel):
    contacts: list[WhatsAppContactResponse]
    omitted_ambiguous: int = 0


class ImportWhatsAppContactsRequest(BaseModel):
    phones: list[str] = Field(..., min_length=1, max_length=500)


class ImportWhatsAppContactsResponse(ImportLeadsResponse):
    rejected: list[str] = Field(default_factory=list)


class LeadResponse(BaseModel):
    id: uuid.UUID
    phone_e164: str
    name: str
    source: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class EnqueueCampaignRequest(BaseModel):
    lead_ids: Optional[list[uuid.UUID]] = None
    limit: Optional[int] = Field(None, ge=1, le=100)
    name: str = Field("Campaña", min_length=1, max_length=80)
    message_template: Optional[str] = Field(None, min_length=3, max_length=1000)
    bait_template_id: Optional[uuid.UUID] = None


class EnqueueCampaignResponse(BaseModel):
    ok: bool
    campaign_id: Optional[str] = None
    queued: int = 0
    skipped: int = 0
    error: Optional[str] = None


class OutboundLimitsResponse(BaseModel):
    can_send_bait: bool
    block_reason: Optional[str] = None
    disclaimer_accepted: bool
    trial_bait_used: int
    trial_bait_limit: int
    trial_bait_remaining: int
    daily_bait_sent: int
    daily_bait_limit: int
    is_trial: Optional[bool] = None
    daily_remaining: Optional[int] = None


class QueueStatsResponse(BaseModel):
    queue_pending: int
    queue_processing: int
    leads_pending: int
    outbound_paused: bool


class CampaignResponse(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    total_queued: int
    total_sent: int
    total_skipped: int
    total_failed: int
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PauseOutboundRequest(BaseModel):
    paused: bool

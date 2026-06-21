from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class WhatsAppConnectResponse(BaseModel):
    instance_name: str
    status: str
    qr_base64: Optional[str] = None
    qr_updated_at: Optional[datetime] = None
    phone_number: Optional[str] = None


class WhatsAppStatusResponse(BaseModel):
    instance_name: str
    status: str
    phone_number: Optional[str] = None
    qr_base64: Optional[str] = None
    qr_updated_at: Optional[datetime] = None
    last_connected_at: Optional[datetime] = None
    last_disconnected_at: Optional[datetime] = None


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    contact_phone: str
    contact_jid: Optional[str] = None
    contact_name: str
    mode: str
    ai_active: bool
    bait_sent: bool
    status: str
    unread_count: int
    last_message_at: Optional[datetime]
    created_at: datetime


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    direction: str
    source: str
    body: str
    status: str
    evolution_message_id: Optional[str]
    created_at: datetime


class ConversationModeUpdate(BaseModel):
    mode: str = Field(pattern="^(auto|manual)$")


class ConversationAiUpdate(BaseModel):
    ai_active: bool


class WhatsAppSyncResponse(BaseModel):
    status: str = "completed"
    conversations_imported: int = 0
    messages_imported: int = 0
    evolution_chats: int = 0
    evolution_contacts: int = 0
    message: Optional[str] = None

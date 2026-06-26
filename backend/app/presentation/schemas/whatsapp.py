from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.shared.core.phone import format_display_phone, resolve_display_name


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
    is_archived: bool = False
    unread_count: int
    last_message_at: Optional[datetime]
    created_at: datetime
    display_name: str = ""
    display_phone: str = ""


def serialize_conversation(conversation, *, display_name_override: str | None = None) -> dict:
    jid = getattr(conversation, "contact_jid", None) or ""
    resolved_name = display_name_override
    if not resolved_name:
        resolved_name = resolve_display_name(
            conversation.contact_name,
            conversation.contact_phone,
            contact_jid=jid,
        )
    response = ConversationResponse.model_validate(conversation).model_copy(
        update={
            "display_name": resolved_name,
            "display_phone": format_display_phone(conversation.contact_phone),
        }
    )
    return response.model_dump(mode="json")


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
    contacts_enriched: int = 0
    names_fixed: int = 0
    phones_fixed: int = 0
    evolution_chats: int = 0
    evolution_contacts: int = 0
    evolution_message_chats: int = 0
    profile_names_fixed: int = 0
    profile_fetched: int = 0
    message: Optional[str] = None


class WhatsAppChatsDebugResponse(BaseModel):
    generated_at: str
    timing_ms: dict[str, int]
    errors: list[str]
    session: dict
    evolution_api: dict
    evolution_db: dict
    names_lookup: dict
    app_db: dict
    sync_queue: dict
    sample_missing_names: list[dict]
    hints: list[str]


class WhatsAppSyncDebugResponse(BaseModel):
    generated_at: str
    timing_ms: dict[str, int]
    errors: list[str]
    hints: list[str]
    session: dict
    urls: dict
    webhook: dict
    messages: dict


class ConversationLiveSyncResponse(BaseModel):
    imported: int
    message_count: int
    messages: list[MessageResponse]

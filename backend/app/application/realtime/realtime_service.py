from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

from app.application.billing.tenant_service import is_placeholder_business_name
from app.domain.entities import Conversation, Message, Tenant
from app.infrastructure.cache.redis_client import publish_event, tenant_cache_key
from app.presentation.schemas.whatsapp import ConversationResponse, MessageResponse, serialize_conversation


def _channel(tenant_id: str) -> str:
    return tenant_cache_key(tenant_id, "events")


def publish_panel_event(tenant_id: str | UUID, payload: dict[str, Any]) -> None:
    publish_event(_channel(str(tenant_id)), json.dumps(payload, default=str))


def publish_message_event(
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    *,
    event_type: str,
) -> None:
    publish_panel_event(
        tenant.id,
        {
            "type": event_type,
            "conversation": serialize_conversation(conversation),
            "message": MessageResponse.model_validate(message).model_dump(mode="json"),
        },
    )


def publish_conversation_updated(tenant_id: str | UUID, conversation: Conversation) -> None:
    publish_panel_event(
        tenant_id,
        {
            "type": "conversation.updated",
            "conversation": serialize_conversation(conversation),
        },
    )


def publish_whatsapp_status(
    tenant_id: str | UUID,
    *,
    status: str,
    qr_base64: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> None:
    publish_panel_event(
        tenant_id,
        {
            "type": "whatsapp.status",
            "status": status,
            "qr_base64": qr_base64,
            "phone_number": phone_number,
        },
    )


def publish_tenant_settings(tenant: Tenant) -> None:
    publish_panel_event(
        tenant.id,
        {
            "type": "tenant.settings",
            "business_name": tenant.business_name,
            "business_name_is_placeholder": is_placeholder_business_name(tenant.business_name),
            "ai_global_enabled": tenant.ai_global_enabled,
            "whatsapp_status": tenant.whatsapp_status,
        },
    )

from __future__ import annotations

from app.application.ai.ai_classifier_service import classify_inbound_message
from app.application.conversations.interest_alert_service import is_alert_phone
from app.domain.entities import Conversation, Tenant
from app.domain.entities.enums import ConversationMode


def maybe_auto_enable_ai_for_inbound(
    *,
    tenant: Tenant,
    conversation: Conversation,
    body: str,
) -> bool:
    """
    Activa IA automáticamente sin tocar cada chat:
    - Carnada enviada → siempre al responder.
    - Contacto nuevo (no importado) → si el mensaje parece consulta de negocio.
    - Chat importado (amigos/agenda) → nunca, salvo carnada.
    - Nunca si el agente ya decidió la IA de este chat o está en modo manual.
    - Nunca en el chat del número que recibe las alertas (el propio dueño).
    """
    if not tenant.ai_global_enabled or conversation.ai_active:
        return False
    if conversation.ai_set_by_agent or conversation.mode == ConversationMode.MANUAL.value:
        return False
    if is_alert_phone(tenant, conversation.contact_phone):
        return False

    if conversation.bait_sent:
        conversation.ai_active = True
        return True

    if conversation.imported_legacy:
        return False

    classification = classify_inbound_message(
        (body or "").strip(),
        business_name=tenant.business_name,
    )
    category = classification.get("category", "")
    if category in ("duda", "interesado"):
        conversation.ai_active = True
        return True
    return False

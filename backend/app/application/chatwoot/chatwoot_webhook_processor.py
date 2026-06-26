from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.application.chatwoot.chatwoot_inbox_sync import sync_chatwoot_conversation
from app.application.chatwoot.chatwoot_service import resolve_tenant_by_inbox_id
from app.config import settings

log = logging.getLogger(__name__)


def process_chatwoot_webhook(db: Session, payload: dict) -> None:
    """Webhook liviano: pide a Chatwoot API la conversación (como willph/Evolution)."""
    if not settings.chatwoot_enabled:
        return

    event = str(payload.get("event") or "").strip()
    if event not in {
        "message_created",
        "message_updated",
        "conversation_created",
        "conversation_updated",
    }:
        return

    conversation_data = payload.get("conversation")
    if not isinstance(conversation_data, dict) and event.startswith("conversation_"):
        conversation_data = payload if isinstance(payload, dict) else {}
    if not isinstance(conversation_data, dict):
        conversation_data = {}

    inbox_id_raw = conversation_data.get("inbox_id")
    if inbox_id_raw is None and isinstance(payload.get("inbox"), dict):
        inbox_id_raw = payload["inbox"].get("id")
    try:
        inbox_id = int(inbox_id_raw)
    except (TypeError, ValueError):
        return

    resolved = resolve_tenant_by_inbox_id(db, inbox_id)
    if resolved is None:
        return
    tenant, session = resolved
    if session.active_connection_id is None:
        return

    cw_conv_id: Optional[int] = None
    try:
        cw_conv_id = int(conversation_data.get("id") or payload.get("conversation_id"))
    except (TypeError, ValueError):
        pass

    if cw_conv_id is None:
        return

    sync_chatwoot_conversation(
        db,
        tenant=tenant,
        session=session,
        chatwoot_conversation_id=cw_conv_id,
    )
    db.commit()

    if event == "message_created":
        msg = payload if isinstance(payload, dict) else {}
        if str(msg.get("message_type") or "").lower() == "incoming":
            from app.domain.entities import Message

            cw_msg_id = msg.get("id")
            if cw_msg_id:
                row = (
                    db.query(Message)
                    .filter(
                        Message.tenant_id == tenant.id,
                        Message.chatwoot_message_id == int(cw_msg_id),
                    )
                    .first()
                )
                if row:
                    from app.application.ai.ai_queue_service import flush_pending_ai_replies

                    flush_pending_ai_replies([(tenant.id, row.conversation_id, row.id)])

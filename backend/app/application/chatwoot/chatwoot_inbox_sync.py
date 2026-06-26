from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.application.messaging.message_service import _find_existing_message, _save_message
from app.application.realtime.realtime_service import publish_conversation_updated
from app.config import settings
from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
from app.domain.entities.enums import MessageDirection, MessageSource, MessageStatus
from app.infrastructure.chatwoot.chatwoot_client import ChatwootAPIError, chatwoot_client
from app.shared.core.phone import is_valid_whatsapp_phone, normalize_phone

log = logging.getLogger(__name__)


def _parse_ts(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError, OSError):
        return None


def _contact_from_conversation(cw_conv: dict) -> tuple[str, str]:
    meta = cw_conv.get("meta") if isinstance(cw_conv.get("meta"), dict) else {}
    sender = meta.get("sender") if isinstance(meta.get("sender"), dict) else {}
    phone = ""
    for raw in (sender.get("phone_number"), sender.get("identifier")):
        if not raw:
            continue
        norm = normalize_phone(str(raw))
        if norm and is_valid_whatsapp_phone(norm):
            phone = norm
            break
    name = str(sender.get("name") or "").strip()[:200]
    return phone, name


def _find_conversation_for_chatwoot(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    cw_id: int,
    phone: str,
) -> Optional[Conversation]:
    """Un chat de Chatwoot = una conversación (por chatwoot_conversation_id o teléfono exacto)."""
    if session.active_connection_id is None:
        return None

    base = db.query(Conversation).filter(
        Conversation.tenant_id == tenant.id,
        Conversation.whatsapp_connection_id == session.active_connection_id,
    )
    by_cw = base.filter(Conversation.chatwoot_conversation_id == cw_id).first()
    if by_cw is not None:
        return by_cw
    if phone and is_valid_whatsapp_phone(phone):
        norm = normalize_phone(phone)
        return base.filter(Conversation.contact_phone == norm).first()
    return None


def _upsert_conversation_from_chatwoot(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    cw_conv: dict,
) -> Optional[Conversation]:
    if session.active_connection_id is None:
        return None
    try:
        cw_id = int(cw_conv.get("id"))
    except (TypeError, ValueError):
        return None

    phone, name = _contact_from_conversation(cw_conv)
    if not phone:
        return None

    conv = _find_conversation_for_chatwoot(
        db,
        tenant=tenant,
        session=session,
        cw_id=cw_id,
        phone=phone,
    )
    if conv is None:
        conv = Conversation(
            tenant_id=tenant.id,
            contact_phone=phone,
            contact_name=name or phone,
            whatsapp_connection_id=session.active_connection_id,
            chatwoot_conversation_id=cw_id,
        )
        db.add(conv)

    conv.chatwoot_conversation_id = cw_id
    conv.whatsapp_connection_id = session.active_connection_id
    if name and (not conv.contact_name or conv.contact_name == conv.contact_phone):
        conv.contact_name = name
    if is_valid_whatsapp_phone(phone):
        conv.contact_phone = normalize_phone(phone)

    db.flush()
    return conv


def _import_messages(
    db: Session,
    *,
    tenant: Tenant,
    conv: Conversation,
    cw_messages: list[dict],
) -> int:
    imported = 0
    for item in reversed(cw_messages):
        if not isinstance(item, dict):
            continue
        try:
            cw_msg_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if (
            db.query(Message)
            .filter(Message.tenant_id == tenant.id, Message.chatwoot_message_id == cw_msg_id)
            .first()
        ):
            continue

        body = str(item.get("content") or "").strip() or "[mensaje]"
        msg_type = str(item.get("message_type") or "").lower()
        from_me = msg_type in {"outgoing", "template"}
        ts = _parse_ts(item.get("created_at"))

        if (
            _find_existing_message(
                db,
                tenant_id=tenant.id,
                conversation_id=conv.id,
                evolution_message_id=f"cw:{cw_msg_id}",
                body=body,
                created_at=ts,
            )
            is not None
        ):
            continue

        msg = _save_message(
            db,
            tenant=tenant,
            conversation=conv,
            direction=MessageDirection.OUT.value if from_me else MessageDirection.IN.value,
            source=MessageSource.AGENT.value if from_me else MessageSource.CONTACT.value,
            body=body,
            status=MessageStatus.SENT.value if from_me else MessageStatus.RECEIVED.value,
            evolution_message_id=f"cw:{cw_msg_id}",
            increment_unread=False,
            created_at=ts,
            publish=False,
        )
        msg.chatwoot_message_id = cw_msg_id
        imported += 1

    return imported


def _trim_messages(msgs: list, *, limit: int) -> list:
    if limit <= 0 or len(msgs) <= limit:
        return msgs
    return msgs[-limit:]


def sync_chatwoot_conversation(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    chatwoot_conversation_id: int,
) -> int:
    """Importa una conversación desde Chatwoot API (fuente de verdad, sin duplicar)."""
    meta = (session.metadata_json or {}).get("chatwoot") or {}
    inbox_id = meta.get("inbox_id")
    if not inbox_id:
        return 0

    try:
        cw_conv = chatwoot_client.get_conversation(chatwoot_conversation_id)
        messages_payload = chatwoot_client.list_messages(chatwoot_conversation_id)
    except ChatwootAPIError as exc:
        log.debug("sync_chatwoot_conversation %s: %s", chatwoot_conversation_id, exc)
        return 0

    conv = _upsert_conversation_from_chatwoot(db, tenant=tenant, session=session, cw_conv=cw_conv)
    if conv is None:
        return 0

    msgs = messages_payload if isinstance(messages_payload, list) else []
    if isinstance(messages_payload, dict):
        msgs = messages_payload.get("payload") or []

    msg_limit = settings.chatwoot_inbox_messages_per_chat
    imported = _import_messages(
        db, tenant=tenant, conv=conv, cw_messages=_trim_messages(msgs, limit=msg_limit)
    )
    if imported:
        publish_conversation_updated(tenant.id, conv)
    return imported


def sync_chatwoot_inbox(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    message_limit: Optional[int] = None,
    max_chats: Optional[int] = None,
) -> dict:
    """Sincroniza inbox desde Chatwoot en lotes pequeños (evita saturar Evolution/Node)."""
    meta = (session.metadata_json or {}).get("chatwoot") or {}
    inbox_id = meta.get("inbox_id")
    if not inbox_id:
        return {"conversations": 0, "messages": 0}

    per_chat = message_limit if message_limit is not None else settings.chatwoot_inbox_messages_per_chat
    chat_cap = max_chats if max_chats is not None else settings.chatwoot_inbox_max_chats_per_sync

    try:
        conversations = chatwoot_client.list_inbox_conversations(int(inbox_id))
    except ChatwootAPIError as exc:
        log.warning("sync_chatwoot_inbox tenant=%s: %s", tenant.id, exc)
        return {"conversations": 0, "messages": 0}

    conv_count = 0
    msg_count = 0
    for cw_conv in conversations[:chat_cap]:
        if not isinstance(cw_conv, dict):
            continue
        conv = _upsert_conversation_from_chatwoot(
            db, tenant=tenant, session=session, cw_conv=cw_conv
        )
        if conv is None:
            continue
        conv_count += 1
        try:
            cw_id = int(cw_conv.get("id"))
            raw_msgs = chatwoot_client.list_messages(cw_id, limit=per_chat)
            msgs = raw_msgs if isinstance(raw_msgs, list) else (raw_msgs.get("payload") or [])
            msg_count += _import_messages(
                db,
                tenant=tenant,
                conv=conv,
                cw_messages=_trim_messages(msgs, limit=per_chat),
            )
        except (ChatwootAPIError, TypeError, ValueError):
            continue
        if conv_count % 4 == 0:
            time.sleep(0.25)

    if conv_count:
        db.commit()
        try:
            from app.application.realtime.realtime_service import publish_panel_event

            publish_panel_event(
                tenant.id,
                {
                    "type": "sync.completed",
                    "status": "completed",
                    "source": "chatwoot",
                    "conversations_imported": conv_count,
                    "messages_imported": msg_count,
                },
            )
        except Exception:
            pass
    return {"conversations": conv_count, "messages": msg_count}

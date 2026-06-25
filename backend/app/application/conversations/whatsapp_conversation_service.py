from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
from app.domain.entities.enums import WhatsAppStatus
from app.application.realtime.realtime_service import publish_panel_event

log = logging.getLogger(__name__)


def begin_whatsapp_connection(session: WhatsAppSession, *, owner_jid: Optional[str] = None) -> UUID:
    """Nueva vinculación QR — los chats anteriores no aplican a esta sesión."""
    connection_id = uuid.uuid4()
    session.active_connection_id = connection_id
    session.connection_started_at = datetime.now(timezone.utc)
    if owner_jid:
        session.bound_owner_jid = str(owner_jid)
    return connection_id


def purge_all_tenant_whatsapp_conversations(db: Session, *, tenant_id: UUID) -> int:
    """Elimina todos los chats WA del tenant y sus mensajes."""
    conv_ids = [
        row[0]
        for row in db.query(Conversation.id)
        .filter(Conversation.tenant_id == tenant_id)
        .all()
    ]
    if conv_ids:
        db.query(Message).filter(Message.conversation_id.in_(conv_ids)).delete(
            synchronize_session=False
        )
    deleted = (
        db.query(Conversation)
        .filter(Conversation.tenant_id == tenant_id)
        .delete(synchronize_session=False)
    )
    log.info("Chats WA tenant=%s eliminados count=%s", tenant_id, deleted)
    return deleted


def purge_whatsapp_conversations(
    db: Session,
    *,
    tenant_id: UUID,
    session: WhatsAppSession,
) -> int:
    """Elimina chats de la vinculación WhatsApp actual (no son datos permanentes de cuenta)."""
    query = db.query(Conversation).filter(Conversation.tenant_id == tenant_id)
    if session.active_connection_id:
        query = query.filter(
            Conversation.whatsapp_connection_id == session.active_connection_id
        )
    else:
        query = query.filter(Conversation.whatsapp_connection_id.isnot(None))

    deleted = query.delete(synchronize_session=False)
    session.active_connection_id = None
    log.info(
        "Chats WA eliminados tenant=%s count=%s",
        tenant_id,
        deleted,
    )
    return deleted


def start_new_whatsapp_binding(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    instance_name: str,
    owner_jid: Optional[str] = None,
) -> UUID:
    """Nueva vinculación (QR o cambio de celular): borra chats viejos y cache Evolution."""
    from app.config import settings
    from app.infrastructure.evolution.evolution_store import purge_instance_stored_data

    purge_all_tenant_whatsapp_conversations(db, tenant_id=tenant.id)
    if settings.evolution_database_url:
        purge_instance_stored_data(settings.evolution_database_url, instance_name)
    try:
        from app.infrastructure.cache.redis_client import get_redis

        get_redis().delete(f"tenant:{tenant.id}:sync_running")
    except Exception:
        pass
    connection_id = begin_whatsapp_connection(session, owner_jid=owner_jid)
    notify_conversations_cleared(tenant.id)
    log.info(
        "Nueva vinculación WA tenant=%s connection=%s owner=%s phone_was=%s",
        tenant.id,
        connection_id,
        owner_jid,
        session.phone_number,
    )
    return connection_id


def ensure_whatsapp_binding_ready(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    owner_jid: Optional[str] = None,
) -> bool:
    """Repara estado inconsistente (sin connection_started_at o owner distinto)."""
    if tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value:
        return False
    from app.shared.core.phone import jid_to_phone

    owner = owner_jid or session.bound_owner_jid
    new_phone = jid_to_phone(str(owner)) if owner else None
    owner_changed = bool(
        session.bound_owner_jid
        and owner
        and session.bound_owner_jid != str(owner)
    )
    phone_changed = bool(
        session.phone_number
        and new_phone
        and session.phone_number != new_phone
        and session.bound_owner_jid
        and jid_to_phone(session.bound_owner_jid) != new_phone
    )
    missing_binding = (
        session.active_connection_id is None or session.connection_started_at is None
    )
    if not missing_binding and not owner_changed and not phone_changed:
        return False

    log.warning(
        "Reparando vinculación WA tenant=%s (conn=%s started=%s bound=%s phone=%s)",
        tenant.id,
        session.active_connection_id,
        session.connection_started_at,
        session.bound_owner_jid,
        session.phone_number,
    )
    if owner_changed or phone_changed:
        start_new_whatsapp_binding(
            db,
            tenant=tenant,
            session=session,
            instance_name=session.instance_name,
            owner_jid=owner,
        )
    elif session.active_connection_id is None:
        existing = (
            db.query(Conversation.whatsapp_connection_id)
            .filter(
                Conversation.tenant_id == tenant.id,
                Conversation.whatsapp_connection_id.isnot(None),
            )
            .order_by(Conversation.created_at.desc())
            .first()
        )
        if existing and existing[0]:
            session.active_connection_id = existing[0]
            if session.connection_started_at is None:
                session.connection_started_at = datetime.now(timezone.utc)
        else:
            begin_whatsapp_connection(session, owner_jid=owner)
    elif session.connection_started_at is None:
        session.connection_started_at = datetime.now(timezone.utc)
    return True


def notify_conversations_cleared(tenant_id: UUID) -> None:
    publish_panel_event(tenant_id, {"type": "conversations.cleared"})


def active_connection_id(session: Optional[WhatsAppSession]) -> Optional[UUID]:
    if session is None:
        return None
    return session.active_connection_id


def conversations_visible_for_tenant(
    db: Session,
    *,
    tenant: Tenant,
    session: Optional[WhatsAppSession],
) -> bool:
    if tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value:
        return False
    if session is None or session.active_connection_id is None:
        return False
    return True


def _owner_user_part(jid: str) -> str:
    return str(jid).split("@")[0].split(":")[0]


def needs_new_whatsapp_binding(
    session: WhatsAppSession,
    *,
    previous_status: str,
    mapped_status: str,
    owner_jid: Optional[str],
) -> bool:
    from app.shared.core.phone import jid_to_phone

    if mapped_status != WhatsAppStatus.CONNECTED.value:
        return False
    if previous_status != WhatsAppStatus.CONNECTED.value:
        return True
    if session.active_connection_id is None:
        return True
    if owner_jid:
        owner_str = str(owner_jid)
        new_phone = jid_to_phone(owner_str)
        if new_phone and session.phone_number and new_phone != session.phone_number:
            return True
        if session.bound_owner_jid:
            old_phone = jid_to_phone(session.bound_owner_jid)
            if new_phone and old_phone and new_phone != old_phone:
                return True
            if _owner_user_part(owner_str) != _owner_user_part(session.bound_owner_jid):
                return True
    return False

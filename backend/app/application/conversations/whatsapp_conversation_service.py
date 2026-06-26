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
        purge_instance_stored_data(settings.evolution_database_url, session.instance_name)
    try:
        from app.application.sync.tenant_sync_state import clear_tenant_sync_state

        clear_tenant_sync_state(tenant.id)
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


def pick_merge_primary(
    left: Conversation,
    right: Conversation,
    *,
    owner_names: Optional[set[str]] = None,
) -> tuple[Conversation, Conversation]:
    """Elige qué conversación conservar al fusionar (@lid con nombre > número)."""
    from app.shared.core.phone import (
        is_lid_placeholder,
        is_owner_display_name,
        is_placeholder_contact_name,
        is_untrusted_contact_phone,
        is_valid_whatsapp_phone,
        phone_trust_rank,
    )

    owner_names = owner_names or set()

    def _has_trusted_phone(conv: Conversation) -> bool:
        return (
            is_valid_whatsapp_phone(conv.contact_phone)
            and not is_lid_placeholder(conv.contact_phone)
            and not is_untrusted_contact_phone(conv.contact_phone)
        )

    def _is_lid_only_identity(conv: Conversation) -> bool:
        return bool((conv.contact_jid or "").endswith("@lid")) and not _has_trusted_phone(conv)

    if _has_trusted_phone(left) and _is_lid_only_identity(right):
        return left, right
    if _has_trusted_phone(right) and _is_lid_only_identity(left):
        return right, left

    def score(conv: Conversation) -> tuple[int, int, int]:
        name = str(conv.contact_name or "").strip()
        if name and is_owner_display_name(name, owner_names):
            return (-1, 0, 0)
        named = 0
        if name and not is_placeholder_contact_name(name, conv.contact_phone):
            named = 2
        lid_score = 0
        if (conv.contact_jid or "").endswith("@lid"):
            lid_score = 2
        elif is_lid_placeholder(conv.contact_phone):
            lid_score = 1
        phone_score = phone_trust_rank(conv.contact_phone)
        return (named, lid_score, phone_score)

    left_score = score(left)
    right_score = score(right)
    if left_score != right_score:
        return (left, right) if left_score > right_score else (right, left)

    if left.created_at and right.created_at and left.created_at <= right.created_at:
        return left, right
    return right, left


def merge_conversations(
    db: Session,
    *,
    tenant_id: UUID,
    primary: Conversation,
    secondary: Conversation,
    owner_names: Optional[set[str]] = None,
) -> None:
    """Mueve mensajes de secondary → primary y borra secondary."""
    from app.application.messaging.message_service import _find_existing_message
    from app.application.sync.contact_identity_service import apply_identity_to_conversation
    from app.shared.core.phone import (
        is_owner_display_name,
        is_placeholder_contact_name,
        is_untrusted_contact_phone,
        is_valid_whatsapp_phone,
        phone_trust_rank,
    )

    owner_names = owner_names or set()

    if primary.id == secondary.id:
        return

    for msg in (
        db.query(Message)
        .filter(Message.conversation_id == secondary.id)
        .all()
    ):
        dup = _find_existing_message(
            db,
            tenant_id=tenant_id,
            conversation_id=primary.id,
            evolution_message_id=msg.evolution_message_id or "",
            body=msg.body,
            created_at=msg.created_at,
        )
        if dup:
            db.delete(msg)
        else:
            msg.conversation_id = primary.id

    if secondary.last_message_at and (
        not primary.last_message_at or secondary.last_message_at > primary.last_message_at
    ):
        primary.last_message_at = secondary.last_message_at
    if secondary.is_archived:
        primary.is_archived = True
    primary.unread_count = (primary.unread_count or 0) + (secondary.unread_count or 0)

    secondary_phone = secondary.contact_phone
    secondary_name = secondary.contact_name
    secondary_jid = secondary.contact_jid or ""

    db.delete(secondary)
    db.flush()

    def _better_phone(a: str, b: str) -> str:
        if phone_trust_rank(a) > phone_trust_rank(b):
            return a
        if phone_trust_rank(b) > phone_trust_rank(a):
            return b
        if is_valid_whatsapp_phone(a):
            return a
        return b

    merged_phone = _better_phone(primary.contact_phone, secondary_phone)

    apply_identity_to_conversation(
        primary,
        contact_phone=merged_phone if is_valid_whatsapp_phone(merged_phone) else primary.contact_phone,
        contact_name=(
            secondary_name
            if is_placeholder_contact_name(primary.contact_name, primary.contact_phone)
            and not (
                owner_names
                and is_owner_display_name(str(secondary_name or ""), owner_names)
            )
            else primary.contact_name
        ),
        contact_jid=secondary_jid or primary.contact_jid or "",
    )


def repair_duplicate_conversations(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    instance_name: str = "",
    owner_names: Optional[set[str]] = None,
) -> int:
    """Fusiona chats duplicados — desactivado en modo solo Chatwoot."""
    from app.application.chatwoot.chatwoot_service import chatwoot_sync_mode

    if chatwoot_sync_mode():
        return 0

    from app.application.whatsapp.whatsapp_status import sanitize_leaked_owner_name

    from app.config import settings
    from app.infrastructure.evolution.evolution_store import fetch_bidirectional_lid_mappings
    from app.shared.core.phone import (
        is_lid_placeholder,
        is_placeholder_contact_name,
        is_untrusted_contact_phone,
        is_valid_whatsapp_phone,
        lid_jid_from_lid_phone,
        normalize_phone,
    )

    rows = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.whatsapp_connection_id == connection_id,
        )
        .order_by(Conversation.created_at.asc())
        .all()
    )
    if len(rows) < 2:
        return 0

    owner_names = owner_names or set()
    for conv in rows:
        sanitize_leaked_owner_name(conv, owner_names)

    lid_to_phone, phone_to_lid = ({}, {})
    from app.application.conversations.contact_resolver_service import _load_link_maps

    app_lid, app_phone = _load_link_maps(
        db, tenant_id=tenant_id, whatsapp_connection_id=connection_id
    )
    lid_to_phone.update(app_lid)
    phone_to_lid.update(app_phone)

    if instance_name and settings.evolution_database_url:
        evo_lid, evo_phone = fetch_bidirectional_lid_mappings(
            settings.evolution_database_url, instance_name
        )
        lid_to_phone.update(evo_lid)
        phone_to_lid.update(evo_phone)

    # Mapeos aprendidos en chats que ya tienen @lid y teléfono juntos.
    for conv in rows:
        jid = str(conv.contact_jid or "")
        phone = str(conv.contact_phone or "")
        if jid.endswith("@lid") and is_valid_whatsapp_phone(phone):
            norm = normalize_phone(phone)
            lid_to_phone.setdefault(jid, norm)
            from app.shared.core.phone import phone_to_evolution_number

            digits = phone_to_evolution_number(norm)
            phone_to_lid.setdefault(digits, jid)
            phone_to_lid.setdefault(norm, jid)

    merged = 0
    alive = {conv.id: conv for conv in rows}

    def _do_merge(a: Conversation, b: Conversation) -> None:
        nonlocal merged
        if a.id not in alive or b.id not in alive or a.id == b.id:
            return
        primary, secondary = pick_merge_primary(a, b, owner_names=owner_names)
        merge_conversations(
            db,
            tenant_id=tenant_id,
            primary=primary,
            secondary=secondary,
            owner_names=owner_names,
        )
        alive.pop(secondary.id, None)
        merged += 1

    by_phone: dict[str, Conversation] = {}
    for conv in list(alive.values()):
        if is_valid_whatsapp_phone(conv.contact_phone):
            phone = normalize_phone(conv.contact_phone)
            if phone in by_phone:
                _do_merge(by_phone[phone], conv)
            else:
                by_phone[phone] = alive.get(conv.id, conv)

    for conv in list(alive.values()):
        if not is_lid_placeholder(conv.contact_phone):
            continue
        lid_jid = conv.contact_jid or lid_jid_from_lid_phone(conv.contact_phone)
        mapped_phone = lid_to_phone.get(lid_jid or "") if lid_jid else None
        if mapped_phone and is_valid_whatsapp_phone(mapped_phone):
            phone = normalize_phone(mapped_phone)
            other = by_phone.get(phone)
            if other:
                _do_merge(conv, other)

    # @lid con nombre + chat solo número: solo si Evolution/DB enlaza el mismo @lid↔teléfono.
    by_jid: dict[str, list[Conversation]] = {}
    for conv in list(alive.values()):
        jid = str(conv.contact_jid or "")
        if jid.endswith("@lid"):
            by_jid.setdefault(jid, []).append(conv)
    for group in by_jid.values():
        if len(group) < 2:
            continue
        primary = group[0]
        for other in group[1:]:
            if primary.id in alive and other.id in alive:
                primary, secondary = pick_merge_primary(primary, other, owner_names=owner_names)
                _do_merge(primary, secondary)
                primary = alive.get(primary.id, primary)

    # @lid con nombre + chat solo número: solo si Evolution/DB enlaza el mismo @lid↔teléfono.
    trusted_phone = [
        conv
        for conv in list(alive.values())
        if is_valid_whatsapp_phone(conv.contact_phone)
        and not is_untrusted_contact_phone(conv.contact_phone)
    ]
    named_convs = [
        conv
        for conv in list(alive.values())
        if conv.contact_name
        and not is_placeholder_contact_name(conv.contact_name, conv.contact_phone)
    ]
    phone_only_convs = [
        conv
        for conv in list(alive.values())
        if is_valid_whatsapp_phone(conv.contact_phone)
        and not is_untrusted_contact_phone(conv.contact_phone)
        and is_placeholder_contact_name(conv.contact_name, conv.contact_phone)
    ]

    for named_conv in named_convs:
        if named_conv.id not in alive:
            continue
        named_jid = str(named_conv.contact_jid or "")
        for phone_conv in phone_only_convs:
            if phone_conv.id not in alive or named_conv.id == phone_conv.id:
                continue
            phone = normalize_phone(phone_conv.contact_phone)
            should_merge = False
            if named_jid.endswith("@lid"):
                mapped = lid_to_phone.get(named_jid)
                if mapped and normalize_phone(mapped) == phone:
                    should_merge = True
                phone_lid = phone_to_lid.get(phone_to_evolution_number(phone)) or phone_to_lid.get(phone)
                if phone_lid and phone_lid == named_jid:
                    should_merge = True
            if should_merge:
                _do_merge(named_conv, phone_conv)
                break

    return merged


from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import Conversation, Tenant, WhatsAppSession
from app.shared.core.phone import (
    is_lid_placeholder,
    is_placeholder_contact_name,
    is_valid_whatsapp_phone,
    phone_to_evolution_number,
)
from app.application.sync.contact_identity_service import build_contact_names_lookup
from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client
from app.infrastructure.evolution.evolution_store import (
    fetch_bidirectional_lid_mappings,
    fetch_contact_push_name,
    fetch_message_chat_index,
    fetch_stored_chat_name,
    fetch_stored_chats,
    fetch_stored_contacts,
    fetch_stored_counts,
)


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _jid_from_conversation(conversation: Conversation) -> str:
    if conversation.contact_jid:
        return conversation.contact_jid
    if is_valid_whatsapp_phone(conversation.contact_phone):
        return f"{phone_to_evolution_number(conversation.contact_phone)}@s.whatsapp.net"
    if is_lid_placeholder(conversation.contact_phone):
        lid = conversation.contact_phone[4:]
        return f"{lid}@lid" if lid and "@" not in lid else lid
    return ""


def _index_api_rows(rows: list) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        jid = str(row.get("remoteJid") or row.get("id") or "")
        if jid:
            index[jid] = row
    return index


def _missing_name_reason(
    *,
    jid: str,
    find_chats: dict[str, dict],
    find_contacts: dict[str, dict],
    evolution_chat_name: Optional[str],
    evolution_contact_push: Optional[str],
    evolution_message_push: Optional[str],
    profile_name: Optional[str],
    resolved_name: str,
    lid_to_phone: dict[str, str],
    phone_to_lid: dict[str, str],
) -> str:
    if resolved_name:
        return "Nombre resuelto por índice unificado"

    if profile_name:
        return "fetchProfile devolvió nombre — ejecuta ↻ Reparar nombres"

    if jid.endswith("@lid") and not lid_to_phone.get(jid):
        return "Chat @lid sin mapeo a teléfono en mensajes Evolution"

    if jid.endswith("@s.whatsapp.net"):
        digits = jid.split("@")[0]
        if phone_to_lid.get(digits) and find_contacts.get(phone_to_lid[digits]):
            return "Nombre existe en contacto @lid pero no se enlazó al número"

    if find_chats.get(jid, {}).get("pushName") or find_chats.get(jid, {}).get("name"):
        return "findChats tiene nombre pero no se aplicó al guardar"

    if find_contacts.get(jid, {}).get("pushName") or find_contacts.get(jid, {}).get("name"):
        return "findContacts tiene nombre pero no se aplicó al guardar"

    if evolution_chat_name:
        return "Chat.name en Evolution DB sin propagar"

    if evolution_contact_push:
        return "Contact.pushName en Evolution DB sin propagar"

    if evolution_message_push:
        return "pushName en mensajes sin propagar"

    if jid.endswith("@s.whatsapp.net"):
        return "WhatsApp/Evolution no tiene nombre para este número (sin agenda, pushName ni perfil público)"

    if jid.endswith("@lid"):
        return "Contacto @lid sin pushName en Evolution"

    return "Sin JID de contacto — no se pudo diagnosticar"


def build_chats_debug_report(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    sample_limit: int = 25,
) -> dict[str, Any]:
    started = time.perf_counter()
    timing_ms: dict[str, int] = {}
    errors: list[str] = []
    instance_name = session.instance_name
    dsn = settings.evolution_database_url

    find_chats_rows: list = []
    find_contacts_rows: list = []
    t0 = time.perf_counter()
    try:
        find_chats_rows = evolution_client.find_chats(instance_name)
    except EvolutionAPIError as exc:
        errors.append(f"findChats: {exc}")
    timing_ms["find_chats_api"] = _ms(t0)

    t0 = time.perf_counter()
    try:
        find_contacts_rows = evolution_client.find_contacts(instance_name)
    except EvolutionAPIError as exc:
        errors.append(f"findContacts: {exc}")
    timing_ms["find_contacts_api"] = _ms(t0)

    find_chats = _index_api_rows(find_chats_rows)
    find_contacts = _index_api_rows(find_contacts_rows)

    t0 = time.perf_counter()
    evolution_counts = fetch_stored_counts(dsn, instance_name) if dsn else {
        "chats": 0,
        "contacts": 0,
        "messages": 0,
    }
    stored_chats = fetch_stored_chats(dsn, instance_name, limit=5000) if dsn else []
    stored_contacts = fetch_stored_contacts(dsn, instance_name, limit=5000) if dsn else []
    message_index = fetch_message_chat_index(dsn, instance_name, limit=5000) if dsn else []
    lid_to_phone, phone_to_lid = (
        fetch_bidirectional_lid_mappings(dsn, instance_name) if dsn else ({}, {})
    )
    timing_ms["evolution_db"] = _ms(t0)

    t0 = time.perf_counter()
    names_lookup = build_contact_names_lookup(instance_name, use_api=False)
    timing_ms["names_lookup_db"] = _ms(t0)

    t0 = time.perf_counter()
    names_lookup_api = build_contact_names_lookup(instance_name, use_api=True)
    timing_ms["names_lookup_api"] = _ms(t0)

    msg_push_by_jid = {
        str(item.get("remoteJid") or ""): str(item.get("pushName") or "").strip()
        for item in message_index
        if item.get("remoteJid")
    }

    t0 = time.perf_counter()
    conversations = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant.id,
            Conversation.whatsapp_connection_id == session.active_connection_id,
        )
        .order_by(Conversation.last_message_at.desc().nullslast())
        .all()
    )
    timing_ms["app_db"] = _ms(t0)

    placeholder_rows = [
        c
        for c in conversations
        if is_placeholder_contact_name(c.contact_name, c.contact_phone)
    ]
    named_rows = [c for c in conversations if c not in placeholder_rows]

    sample: list[dict[str, Any]] = []
    for conversation in placeholder_rows[: max(1, min(sample_limit, 50))]:
        jid = _jid_from_conversation(conversation)
        api_chat = find_chats.get(jid, {})
        api_contact = find_contacts.get(jid, {})
        evolution_chat_name = (
            fetch_stored_chat_name(dsn, instance_name, jid) if dsn and jid else None
        )
        evolution_contact_push = (
            fetch_contact_push_name(dsn, instance_name, jid) if dsn and jid else None
        )
        evolution_message_push = msg_push_by_jid.get(jid) or None
        profile_name = None
        if jid.endswith("@s.whatsapp.net") and is_valid_whatsapp_phone(conversation.contact_phone):
            try:
                from app.application.sync.profile_name_service import fetch_whatsapp_profile_name

                profile_name = fetch_whatsapp_profile_name(
                    instance_name,
                    conversation.contact_phone,
                    use_cache=False,
                ) or None
            except Exception:
                profile_name = None
        resolved_db = names_lookup.resolve_for_conversation(conversation)
        resolved_api = names_lookup_api.resolve_for_conversation(conversation)
        sample.append(
            {
                "conversation_id": str(conversation.id),
                "contact_phone": conversation.contact_phone,
                "contact_name": conversation.contact_name,
                "contact_jid": conversation.contact_jid or "",
                "remote_jid": jid,
                "resolved_name_db": resolved_db,
                "resolved_name_api": resolved_api,
                "in_find_chats": jid in find_chats,
                "in_find_contacts": jid in find_contacts,
                "find_chats_push_name": api_chat.get("pushName") or api_chat.get("name"),
                "find_contacts_push_name": api_contact.get("pushName")
                or api_contact.get("name"),
                "evolution_chat_name": evolution_chat_name,
                "evolution_contact_push": evolution_contact_push,
                "evolution_message_push": evolution_message_push,
                "fetch_profile_name": profile_name,
                "reason": _missing_name_reason(
                    jid=jid,
                    find_chats=find_chats,
                    find_contacts=find_contacts,
                    evolution_chat_name=evolution_chat_name,
                    evolution_contact_push=evolution_contact_push,
                    evolution_message_push=evolution_message_push,
                    profile_name=profile_name,
                    resolved_name=resolved_api or resolved_db or (profile_name or ""),
                    lid_to_phone=lid_to_phone,
                    phone_to_lid=phone_to_lid,
                ),
            }
        )

    queue_pending = 0
    try:
        from app.application.sync.sync_queue_service import WHATSAPP_SYNC_QUEUE
        from app.infrastructure.cache.redis_client import get_redis

        queue_pending = int(get_redis().llen(WHATSAPP_SYNC_QUEUE))
    except Exception as exc:
        errors.append(f"sync_queue: {exc}")

    find_chats_phone = [
        r for r in find_chats_rows
        if str(r.get("remoteJid") or "").endswith("@s.whatsapp.net")
    ]
    find_chats_lid = [
        r for r in find_chats_rows
        if str(r.get("remoteJid") or "").endswith("@lid")
    ]
    find_contacts_lid = [
        r for r in find_contacts_rows
        if str(r.get("remoteJid") or "").endswith("@lid")
    ]

    timing_ms["total"] = _ms(started)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timing_ms": timing_ms,
        "errors": errors,
        "session": {
            "instance_name": instance_name,
            "whatsapp_status": tenant.whatsapp_status,
            "session_status": session.status,
            "phone_number": session.phone_number,
            "active_connection_id": str(session.active_connection_id)
            if session.active_connection_id
            else None,
            "connection_started_at": session.connection_started_at.isoformat()
            if session.connection_started_at
            else None,
            "evolution_db_configured": bool(dsn),
        },
        "evolution_api": {
            "find_chats_count": len(find_chats_rows),
            "find_contacts_count": len(find_contacts_rows),
            "find_chats_phone_jids": len(find_chats_phone),
            "find_chats_lid_jids": len(find_chats_lid),
            "find_chats_phone_with_push_name": sum(
                1 for r in find_chats_phone if r.get("pushName") or r.get("name")
            ),
            "find_contacts_lid_jids": len(find_contacts_lid),
            "find_contacts_with_push_name": sum(
                1
                for r in find_contacts_rows
                if r.get("pushName") or r.get("name")
            ),
        },
        "evolution_db": {
            **evolution_counts,
            "stored_chats_sampled": len(stored_chats),
            "stored_contacts_sampled": len(stored_contacts),
            "message_index_jids": len(message_index),
            "message_index_with_push_name": sum(
                1 for item in message_index if item.get("pushName")
            ),
            "stored_chats_with_name": sum(
                1 for item in stored_chats if item.get("name")
            ),
            "lid_to_phone_mappings": len(lid_to_phone),
            "phone_to_lid_mappings": len(phone_to_lid),
        },
        "names_lookup": {
            "jid_names_db": len(names_lookup.jid_names),
            "phone_names_db": len(names_lookup.phone_names),
            "jid_names_api": len(names_lookup_api.jid_names),
            "phone_names_api": len(names_lookup_api.phone_names),
        },
        "app_db": {
            "total_conversations": len(conversations),
            "with_real_name": len(named_rows),
            "placeholder_names": len(placeholder_rows),
            "lid_conversations": sum(
                1 for c in conversations if (c.contact_jid or "").endswith("@lid")
            ),
            "phone_conversations": sum(
                1
                for c in conversations
                if is_valid_whatsapp_phone(c.contact_phone)
                and not (c.contact_jid or "").endswith("@lid")
            ),
        },
        "sync_queue": {
            "pending_jobs": queue_pending,
        },
        "sample_missing_names": sample,
        "hints": [
            "Si placeholder_names es alto y find_chats_phone_with_push_name > 0, ejecuta ↻ Reparar nombres.",
            "Si in_find_chats=false y evolution_message_push vacío, WhatsApp no expone nombre para ese chat.",
            "Chats @lid con nombre suelen verse bien; los que fallan suelen ser número sin agenda.",
            "Copia este reporte completo y envíalo para soporte.",
        ],
    }

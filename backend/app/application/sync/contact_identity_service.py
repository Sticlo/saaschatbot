from __future__ import annotations

from typing import Optional

from app.shared.core.phone import (
    is_lid_placeholder,
    is_owner_display_name,
    is_placeholder_contact_name,
    is_valid_whatsapp_phone,
    resolve_contact_phone,
)
from app.domain.entities import Conversation
from app.infrastructure.evolution.evolution_store import (
    fetch_contact_push_name,
    fetch_lid_alt_phone,
    fetch_lid_push_name,
    fetch_stored_chat_name,
)


def build_contacts_index(contacts: list) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for item in contacts:
        if not isinstance(item, dict):
            continue
        jid = str(item.get("remoteJid") or "")
        if not jid:
            continue
        existing = index.get(jid)
        if existing:
            merged = dict(existing)
            for key in ("name", "pushName", "verifiedName", "lastMessageTimestamp", "archived"):
                val = item.get(key)
                if val is None:
                    continue
                if key == "lastMessageTimestamp":
                    try:
                        prev = int(merged.get(key) or 0)
                        merged[key] = max(prev, int(val))
                    except (TypeError, ValueError):
                        merged[key] = val
                elif key == "archived":
                    if item.get("archived") is not None:
                        merged[key] = bool(val)
                elif val and (not merged.get(key) or key == "name"):
                    merged[key] = val
            index[jid] = merged
        else:
            item = dict(item)
            if item.get("pushName") and not item.get("name"):
                item["name"] = item["pushName"]
            index[jid] = item
    return index


def _pick_saved_name(*candidates: str, phone: str = "") -> str:
    """Prioriza nombre de agenda sobre pushName de WhatsApp."""
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and not is_placeholder_contact_name(text, phone):
            return text
    return ""


def resolve_contact_identity(
    remote_jid: str,
    item: dict,
    *,
    contacts_index: dict[str, dict],
    instance_name: str,
    dsn: str,
    key: Optional[dict] = None,
    lid_jid: str = "",
) -> Optional[tuple[str, str, str, Optional[bool]]]:
    """Resuelve (phone, contact_jid, name, is_archived) para un chat WA."""
    last_message = item.get("lastMessage") if isinstance(item.get("lastMessage"), dict) else None
    msg_key = key or ((last_message or {}).get("key") if last_message else None)
    if not isinstance(msg_key, dict):
        msg_key = None

    effective_lid = lid_jid or (remote_jid if remote_jid.endswith("@lid") else "")
    phone = resolve_contact_phone(remote_jid, key=msg_key)
    contact_jid = effective_lid

    if effective_lid and not phone and dsn:
        alt_phone = fetch_lid_alt_phone(dsn, instance_name, effective_lid)
        if alt_phone and is_valid_whatsapp_phone(alt_phone):
            phone = alt_phone

    if effective_lid and not phone:
        phone = f"lid:{effective_lid.split('@')[0]}"

    if not phone:
        return None

    contact_row = contacts_index.get(remote_jid) or contacts_index.get(effective_lid) or {}
    chat_state_name = str(item.get("_chat_state_name") or "")
    contact_names = item.get("_contact_names") or {}
    phone_names = item.get("_phone_names") or {}

    saved_name = _pick_saved_name(
        item.get("name"),
        contact_row.get("name"),
        chat_state_name,
        item.get("_alt_chat_name"),
        contact_names.get(remote_jid),
        contact_names.get(effective_lid) if effective_lid else "",
        phone=phone,
    )
    if not saved_name and dsn:
        saved_name = _pick_saved_name(
            fetch_stored_chat_name(dsn, instance_name, remote_jid),
            fetch_stored_chat_name(dsn, instance_name, effective_lid) if effective_lid else "",
            phone=phone,
        )
        if not saved_name and is_valid_whatsapp_phone(phone):
            from app.shared.core.phone import phone_to_evolution_number

            phone_jid = f"{phone_to_evolution_number(phone)}@s.whatsapp.net"
            saved_name = _pick_saved_name(
                fetch_stored_chat_name(dsn, instance_name, phone_jid),
                contact_names.get(phone_jid),
                phone_names.get(phone),
                phone=phone,
            )

    push_name = _pick_saved_name(
        contact_row.get("pushName"),
        item.get("pushName"),
        (last_message or {}).get("pushName"),
        phone=phone,
    )
    owner_names = item.get("_owner_names") or set()
    if push_name and is_owner_display_name(str(push_name), owner_names):
        push_name = ""
    name = saved_name or push_name

    if is_placeholder_contact_name(str(name), phone) and effective_lid and dsn:
        name = fetch_lid_push_name(dsn, instance_name, effective_lid) or name
    if is_placeholder_contact_name(str(name), phone) and dsn:
        name = fetch_contact_push_name(dsn, instance_name, remote_jid) or name
        if effective_lid and is_placeholder_contact_name(str(name), phone):
            name = fetch_contact_push_name(dsn, instance_name, effective_lid) or name
    if is_placeholder_contact_name(str(name), phone):
        name = phone if is_valid_whatsapp_phone(phone) else "Contacto"

    archived_raw = item.get("archived")
    is_archived: Optional[bool] = bool(archived_raw) if archived_raw is not None else None
    return phone, contact_jid, str(name), is_archived


def lid_jid_from_phone(phone: str) -> str:
    if not is_lid_placeholder(phone):
        return ""
    lid = phone[4:]
    return f"{lid}@lid" if lid and "@" not in lid else lid


def apply_agenda_name(conversation: Conversation, name: str) -> bool:
    if not name or not name.strip():
        return False
    cleaned = name.strip()
    if conversation.contact_name == cleaned:
        return False
    conversation.contact_name = cleaned
    return True


def apply_identity_to_conversation(
    conversation: Conversation,
    *,
    contact_phone: str,
    contact_name: str,
    contact_jid: str = "",
    is_archived: Optional[bool] = None,
) -> bool:
    changed = False
    jid = contact_jid or lid_jid_from_phone(conversation.contact_phone)
    if jid and not conversation.contact_jid:
        conversation.contact_jid = jid
        changed = True
    if contact_phone and is_valid_whatsapp_phone(contact_phone):
        if conversation.contact_phone != contact_phone:
            conversation.contact_phone = contact_phone
            changed = True
    if contact_name:
        old_placeholder = is_placeholder_contact_name(
            conversation.contact_name, conversation.contact_phone
        )
        new_placeholder = is_placeholder_contact_name(
            contact_name, contact_phone or conversation.contact_phone
        )
        if old_placeholder or (not new_placeholder and contact_name != conversation.contact_name):
            if conversation.contact_name != contact_name:
                conversation.contact_name = contact_name
                changed = True
        elif old_placeholder and contact_name:
            if conversation.contact_name != contact_name:
                conversation.contact_name = contact_name
                changed = True
    if is_archived is not None and conversation.is_archived != is_archived:
        conversation.is_archived = is_archived
        changed = True
    return changed


def _build_msg_push_name_index(dsn: str, instance_name: str) -> dict[str, str]:
    """Índice remoteJid→pushName desde mensajes entrantes de Evolution (fallback de nombres)."""
    if not dsn:
        return {}
    try:
        from app.infrastructure.evolution.evolution_store import fetch_message_chat_index

        result: dict[str, str] = {}
        for item in fetch_message_chat_index(dsn, instance_name, limit=5000):
            jid = str(item.get("remoteJid") or "")
            push = str(item.get("pushName") or "").strip()
            if jid and push and jid not in result:
                result[jid] = push
        return result
    except Exception:
        return {}


def enrich_tenant_conversations(
    db,
    *,
    tenant,
    session,
    contacts_index: Optional[dict[str, dict]] = None,
) -> dict[str, int]:
    """Repara nombres/teléfonos @lid en conversaciones ya guardadas."""
    from app.config import settings
    from app.domain.entities import Conversation
    from app.infrastructure.evolution.evolution_store import (
        fetch_contact_names_index,
        fetch_stored_chat_names_index,
        fetch_stored_chats,
        fetch_stored_contacts,
    )

    connection_id = session.active_connection_id
    if connection_id is None:
        return {"enriched": 0, "names_fixed": 0, "phones_fixed": 0}

    dsn = settings.evolution_database_url
    instance_name = session.instance_name
    chat_names = fetch_stored_chat_names_index(dsn, instance_name)
    jid_names, phone_names = fetch_contact_names_index(dsn, instance_name)
    msg_push_names = _build_msg_push_name_index(dsn, instance_name)
    lid_to_phone = {}
    if dsn:
        from app.infrastructure.evolution.evolution_store import fetch_lid_phone_mappings

        lid_to_phone = fetch_lid_phone_mappings(dsn, instance_name)
    if contacts_index is None:
        stored_contacts = fetch_stored_contacts(dsn, instance_name, limit=10000)
        stored_chats = fetch_stored_chats(dsn, instance_name, limit=10000)
        api_rows: list = []
        try:
            from app.infrastructure.evolution.evolution_client import evolution_client

            for row in evolution_client.find_contacts(instance_name) + evolution_client.find_chats(
                instance_name
            ):
                if not isinstance(row, dict):
                    continue
                r = dict(row)
                if r.get("pushName") and not r.get("name"):
                    r["name"] = r["pushName"]
                api_rows.append(r)
        except Exception:
            pass
        contacts_index = build_contacts_index(
            stored_contacts + stored_chats + api_rows
        )

    conversations = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant.id,
            Conversation.whatsapp_connection_id == connection_id,
        )
        .all()
    )

    stats = {"enriched": 0, "names_fixed": 0, "phones_fixed": 0}
    owner_names: set[str] = set()
    if session.bound_owner_jid and dsn:
        from app.infrastructure.evolution.evolution_store import fetch_contact_push_name

        push = fetch_contact_push_name(dsn, instance_name, session.bound_owner_jid)
        if push:
            owner_names.add(push)
    for conversation in conversations:
        remote_jid = conversation.contact_jid or lid_jid_from_phone(conversation.contact_phone)
        if (
            not remote_jid
            and is_valid_whatsapp_phone(conversation.contact_phone)
        ):
            from app.shared.core.phone import phone_to_evolution_number

            remote_jid = f"{phone_to_evolution_number(conversation.contact_phone)}@s.whatsapp.net"
        if not remote_jid:
            if is_placeholder_contact_name(conversation.contact_name, conversation.contact_phone):
                conversation.contact_name = "Contacto"
                stats["names_fixed"] += 1
                stats["enriched"] += 1
            continue

        item = contacts_index.get(remote_jid, {"remoteJid": remote_jid})
        if isinstance(item, dict):
            item = dict(item)
            item["_owner_names"] = owner_names
            item["_contact_names"] = jid_names
            item["_phone_names"] = phone_names
        lid_jid = conversation.contact_jid if (conversation.contact_jid or "").endswith("@lid") else ""
        identity = resolve_contact_identity(
            remote_jid,
            item if isinstance(item, dict) else {"remoteJid": remote_jid, "_owner_names": owner_names},
            contacts_index=contacts_index,
            instance_name=instance_name,
            dsn=dsn,
            lid_jid=lid_jid,
        )
        if not identity:
            if is_placeholder_contact_name(conversation.contact_name, conversation.contact_phone):
                conversation.contact_name = "Contacto"
                if not conversation.contact_jid and remote_jid.endswith("@lid"):
                    conversation.contact_jid = remote_jid
                stats["names_fixed"] += 1
                stats["enriched"] += 1
            continue

        phone, contact_jid, name, is_archived = identity
        old_name = conversation.contact_name
        old_phone = conversation.contact_phone

        from app.shared.core.phone import phone_to_evolution_number

        phone_jid = (
            f"{phone_to_evolution_number(conversation.contact_phone)}@s.whatsapp.net"
            if is_valid_whatsapp_phone(conversation.contact_phone)
            else ""
        )
        agenda_name = (
            chat_names.get(remote_jid)
            or chat_names.get(conversation.contact_jid or "")
            or chat_names.get(phone_jid)
            or jid_names.get(remote_jid)
            or jid_names.get(conversation.contact_jid or "")
            or jid_names.get(phone_jid)
            or (phone_names.get(phone) if is_valid_whatsapp_phone(phone) else "")
            or msg_push_names.get(remote_jid)
            or msg_push_names.get(phone_jid)
            or (msg_push_names.get(conversation.contact_jid or "") if conversation.contact_jid else "")
            or ""
        )
        if not agenda_name and conversation.contact_jid and conversation.contact_jid.endswith("@lid"):
            alt = lid_to_phone.get(conversation.contact_jid)
            if alt:
                alt_jid = f"{phone_to_evolution_number(alt)}@s.whatsapp.net"
                agenda_name = (
                    chat_names.get(alt_jid)
                    or jid_names.get(alt_jid)
                    or msg_push_names.get(alt_jid)
                    or ""
                )

        if agenda_name:
            name = agenda_name

        # Si el nombre resuelto es placeholder (número / Contacto) pero la
        # conversación ya tiene un nombre real, lo conservamos.
        if is_placeholder_contact_name(str(name), phone):
            existing = str(conversation.contact_name or "").strip()
            if existing and not is_placeholder_contact_name(existing, conversation.contact_phone or phone):
                name = existing

        if apply_identity_to_conversation(
            conversation,
            contact_phone=phone,
            contact_name=name,
            contact_jid=contact_jid,
            is_archived=is_archived,
        ):
            stats["enriched"] += 1
        if conversation.contact_name != old_name:
            stats["names_fixed"] += 1
        if conversation.contact_phone != old_phone and is_valid_whatsapp_phone(conversation.contact_phone):
            stats["phones_fixed"] += 1

    return stats

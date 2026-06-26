from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.shared.core.phone import (
    is_lid_placeholder,
    is_owner_display_name,
    is_placeholder_contact_name,
    is_valid_whatsapp_phone,
    lid_jid_from_lid_phone,
    normalize_phone,
    phone_to_evolution_number,
    resolve_contact_phone,
)
from app.domain.entities import Conversation
from app.infrastructure.evolution.evolution_store import (
    fetch_bidirectional_lid_mappings,
    fetch_comprehensive_names_index,
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
    """Returns the first non-placeholder candidate name."""
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
    # WhatsApp pushName takes priority over phone book "saved_name":
    # pushName = what the contact set in their WA profile (reliable).
    # saved_name = what the user typed in their phone book (can be wrong for @lid contacts).
    name = push_name or saved_name

    if is_placeholder_contact_name(str(name), phone) and effective_lid and dsn:
        name = fetch_lid_push_name(dsn, instance_name, effective_lid) or name
    if is_placeholder_contact_name(str(name), phone) and dsn:
        name = fetch_contact_push_name(dsn, instance_name, remote_jid) or name
        if effective_lid and is_placeholder_contact_name(str(name), phone):
            name = fetch_contact_push_name(dsn, instance_name, effective_lid) or name
    if is_placeholder_contact_name(str(name), phone):
        name = phone if is_valid_whatsapp_phone(phone) else ""

    archived_raw = item.get("archived")
    is_archived: Optional[bool] = bool(archived_raw) if archived_raw is not None else None
    return phone, contact_jid, str(name), is_archived


def lid_jid_from_phone(phone: str) -> str:
    if not is_lid_placeholder(phone):
        return ""
    lid = phone[4:]
    return f"{lid}@lid" if lid and "@" not in lid else lid


@dataclass
class ContactNamesLookup:
    jid_names: dict[str, str]
    phone_names: dict[str, str]
    lid_to_phone: dict[str, str]
    phone_to_lid: dict[str, str]

    def resolve_for_conversation(
        self,
        conversation: Conversation,
        *,
        owner_names: Optional[set[str]] = None,
    ) -> str:
        phone = conversation.contact_phone or ""
        contact_jid = conversation.contact_jid or ""
        owner_names = owner_names or set()

        candidates: list[str] = []
        if contact_jid:
            candidates.append(self.jid_names.get(contact_jid, ""))
        if is_valid_whatsapp_phone(phone):
            phone_jid = f"{phone_to_evolution_number(phone)}@s.whatsapp.net"
            candidates.append(self.jid_names.get(phone_jid, ""))
            candidates.append(self.phone_names.get(phone, ""))
            candidates.append(self.phone_names.get(normalize_phone(phone), ""))
            lid_jid = self.phone_to_lid.get(phone_to_evolution_number(phone)) or self.phone_to_lid.get(phone)
            if lid_jid:
                candidates.append(self.jid_names.get(lid_jid, ""))
        elif is_lid_placeholder(phone):
            lid_jid = contact_jid or lid_jid_from_phone(phone)
            if lid_jid:
                candidates.append(self.jid_names.get(lid_jid, ""))
                mapped_phone = self.lid_to_phone.get(lid_jid)
                if mapped_phone:
                    candidates.append(self.phone_names.get(mapped_phone, ""))

        for candidate in candidates:
            cleaned = str(candidate or "").strip()
            if not cleaned:
                continue
            if owner_names and is_owner_display_name(cleaned, owner_names):
                continue
            if not is_placeholder_contact_name(cleaned, phone):
                return cleaned

        existing = str(conversation.contact_name or "").strip()
        if existing and not is_placeholder_contact_name(existing, phone):
            if not owner_names or not is_owner_display_name(existing, owner_names):
                return existing
        return ""


def build_contact_names_lookup(
    instance_name: str,
    *,
    use_api: bool = True,
) -> ContactNamesLookup:
    from app.config import settings
    from app.infrastructure.evolution.evolution_store import register_contact_name

    dsn = settings.evolution_database_url
    jid_names, phone_names = fetch_comprehensive_names_index(dsn, instance_name)
    lid_to_phone, phone_to_lid = fetch_bidirectional_lid_mappings(dsn, instance_name)

    if use_api:
        try:
            from app.infrastructure.evolution.evolution_client import evolution_client

            for row in evolution_client.find_contacts(instance_name) + evolution_client.find_chats(
                instance_name
            ):
                if not isinstance(row, dict):
                    continue
                jid = str(row.get("remoteJid") or row.get("id") or "")
                name = (
                    row.get("name")
                    or row.get("notify")
                    or row.get("pushName")
                    or row.get("verifiedName")
                    or ""
                )
                register_contact_name(jid_names, phone_names, jid, str(name))
        except Exception:
            pass

    msg_push = _build_msg_push_name_index(dsn, instance_name)
    for jid, name in msg_push.items():
        register_contact_name(jid_names, phone_names, jid, name)

    try:
        from app.application.sync.contact_name_cache_service import load_cached_names

        cached_jid, cached_phone = load_cached_names(instance_name)
        for jid, name in cached_jid.items():
            register_contact_name(jid_names, phone_names, jid, name)
        for phone, name in cached_phone.items():
            if phone and name:
                phone_names[phone] = name
    except Exception:
        pass

    for lid_jid, phone in lid_to_phone.items():
        lid_name = jid_names.get(lid_jid)
        if lid_name and phone and phone not in phone_names:
            phone_names[phone] = lid_name
            phone_jid = f"{phone_to_evolution_number(phone)}@s.whatsapp.net"
            jid_names.setdefault(phone_jid, lid_name)

    return ContactNamesLookup(
        jid_names=jid_names,
        phone_names=phone_names,
        lid_to_phone=lid_to_phone,
        phone_to_lid=phone_to_lid,
    )


def apply_names_lookup_to_conversations(
    conversations: list[Conversation],
    lookup: ContactNamesLookup,
    *,
    owner_names: Optional[set[str]] = None,
) -> int:
    """Actualiza contact_name en conversaciones con nombres resueltos desde Evolution."""
    fixed = 0
    for conversation in conversations:
        if not is_placeholder_contact_name(
            conversation.contact_name, conversation.contact_phone
        ):
            continue
        resolved = lookup.resolve_for_conversation(conversation, owner_names=owner_names)
        if resolved and resolved != conversation.contact_name:
            conversation.contact_name = resolved
            fixed += 1
    return fixed


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
        trimmed = contact_name[:200]
        if old_placeholder or (not new_placeholder and trimmed != conversation.contact_name):
            if conversation.contact_name != trimmed:
                conversation.contact_name = trimmed
                changed = True
        elif old_placeholder and trimmed:
            if conversation.contact_name != trimmed:
                conversation.contact_name = trimmed
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
    fetch_profiles: bool = True,
    profile_limit: int = 40,
) -> dict[str, int]:
    """Repara nombres/teléfonos @lid en conversaciones ya guardadas."""
    from app.config import settings
    from app.domain.entities import Conversation
    from app.infrastructure.evolution.evolution_store import (
        fetch_stored_chats,
        fetch_stored_contacts,
    )

    connection_id = session.active_connection_id
    if connection_id is None:
        return {
            "enriched": 0,
            "names_fixed": 0,
            "phones_fixed": 0,
            "profile_fetched": 0,
            "profile_names_fixed": 0,
        }

    dsn = settings.evolution_database_url
    instance_name = session.instance_name
    from app.application.whatsapp.whatsapp_status import build_owner_display_names

    owner_names = build_owner_display_names(session)
    from app.application.sync.contact_name_cache_service import (
        backfill_names_from_evolution_api,
        load_cached_names,
        remember_from_record,
    )

    backfill_names_from_evolution_api(
        instance_name, max_pages=20, owner_names=owner_names
    )
    names_lookup = build_contact_names_lookup(instance_name, use_api=True)
    jid_names = names_lookup.jid_names
    phone_names = names_lookup.phone_names
    lid_to_phone = names_lookup.lid_to_phone
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
                r["name"] = (
                    r.get("name")
                    or r.get("notify")
                    or r.get("pushName")
                    or r.get("verifiedName")
                    or ""
                )
                if r.get("pushName") and not r.get("name"):
                    r["name"] = r["pushName"]
                remember_from_record(instance_name, r, owner_names=owner_names)
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

    stats = {
        "enriched": 0,
        "names_fixed": 0,
        "phones_fixed": 0,
        "profile_fetched": 0,
        "profile_names_fixed": 0,
    }
    from app.application.whatsapp.whatsapp_status import build_owner_display_names

    owner_names = build_owner_display_names(session)
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
                lookup_name = names_lookup.resolve_for_conversation(
                    conversation, owner_names=owner_names
                )
                if lookup_name:
                    conversation.contact_name = lookup_name
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
                if not conversation.contact_jid and remote_jid.endswith("@lid"):
                    conversation.contact_jid = remote_jid
                lookup_name = names_lookup.resolve_for_conversation(
                    conversation, owner_names=owner_names
                )
                if lookup_name:
                    conversation.contact_name = lookup_name
                    stats["names_fixed"] += 1
                    stats["enriched"] += 1
            continue

        phone, contact_jid, name, is_archived = identity
        old_name = conversation.contact_name
        old_phone = conversation.contact_phone

        lookup_name = names_lookup.resolve_for_conversation(
            conversation, owner_names=owner_names
        )
        if lookup_name:
            name = lookup_name

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

    if fetch_profiles:
        from app.application.sync.profile_name_service import enrich_names_from_whatsapp_profiles

        profile_stats = enrich_names_from_whatsapp_profiles(
            conversations,
            instance_name=instance_name,
            owner_names=owner_names,
            limit=profile_limit,
        )
        stats["profile_fetched"] = profile_stats["fetched"]
        stats["profile_names_fixed"] = profile_stats["fixed"]
        if profile_stats["fixed"]:
            stats["names_fixed"] += profile_stats["fixed"]
            stats["enriched"] += profile_stats["fixed"]

    return stats

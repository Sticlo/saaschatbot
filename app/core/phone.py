from __future__ import annotations

import re
from typing import Optional


def jid_to_phone(jid: str) -> str:
    """Convierte 573001234567@s.whatsapp.net → E.164 +573001234567."""
    if not jid:
        return ""
    number = jid.split("@")[0].split(":")[0]
    return normalize_phone(number)


def normalize_phone(raw: str, default_country: str = "57") -> str:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""

    if raw.strip().startswith("+"):
        return f"+{digits}"

    if digits.startswith(default_country):
        return f"+{digits}"

    if len(digits) == 10 and default_country == "57":
        return f"+{default_country}{digits}"

    return f"+{digits}"


def phone_to_evolution_number(phone_e164: str) -> str:
    """Evolution espera número sin '+' ni @."""
    return re.sub(r"\D", "", phone_e164)


def instance_name_for_tenant(tenant_id) -> str:
    return f"t_{str(tenant_id).replace('-', '')[:16]}"


def is_group_or_broadcast_jid(jid: str) -> bool:
    if not jid:
        return True
    lowered = jid.lower()
    return lowered.endswith("@g.us") or "broadcast" in lowered or lowered.endswith("@newsletter")


def is_valid_whatsapp_phone(phone_e164: str) -> bool:
    """Rechaza IDs @lid guardados como teléfono (+71021579251813…)."""
    digits = re.sub(r"\D", "", phone_e164 or "")
    if len(digits) < 10 or len(digits) > 13:
        return False
    if phone_e164.startswith("+710") or digits.startswith("710"):
        return False
    return True


def is_lid_placeholder(phone: str) -> bool:
    return bool(phone) and phone.startswith("lid:")


def is_placeholder_contact_name(name: str, phone: str) -> bool:
    if not name:
        return True
    if name == phone:
        return True
    if is_lid_placeholder(name):
        return True
    lid_digits = phone[4:] if is_lid_placeholder(phone) else ""
    if lid_digits and name in {lid_digits, f"+{lid_digits}"}:
        return True
    return False


def format_display_phone(phone: str) -> str:
    if not phone or is_lid_placeholder(phone):
        return ""
    return phone


def resolve_display_name(name: str, phone: str, *, contact_jid: str = "") -> str:
    if name and not is_placeholder_contact_name(name, phone):
        return name
    display_phone = format_display_phone(phone)
    if display_phone:
        return display_phone
    if contact_jid and contact_jid.endswith("@lid"):
        return "Contacto"
    if is_lid_placeholder(phone) or is_lid_placeholder(name):
        return "Contacto"
    return "Contacto"


def is_owner_jid(
    remote_jid: str,
    *,
    owner_jid: str = "",
    owner_phone: str = "",
) -> bool:
    """True si el JID pertenece al dueño de la sesión WA (chat consigo mismo)."""
    if not remote_jid:
        return False
    if owner_jid and remote_jid == owner_jid:
        return True
    if remote_jid.endswith("@s.whatsapp.net") and owner_phone:
        return jid_to_phone(remote_jid) == normalize_phone(owner_phone)
    return False


def is_owner_display_name(name: str, owner_names: set[str]) -> bool:
    """Evita usar el pushName del dueño como nombre de contacto."""
    if not name or not owner_names:
        return False
    cleaned = name.strip().lower()
    if not cleaned:
        return False
    return cleaned in {n.strip().lower() for n in owner_names if n and n.strip()}


def evolution_send_target(conversation) -> str:
    """Número o JID para Evolution sendText."""
    jid = getattr(conversation, "contact_jid", None) or ""
    if jid and "@" in jid:
        return jid
    phone = conversation.contact_phone or ""
    if phone.startswith("lid:"):
        lid = phone[4:]
        return f"{lid}@lid" if "@" not in lid else lid
    return phone_to_evolution_number(phone)


def resolve_contact_phone(remote_jid: str, *, key: Optional[dict] = None) -> str:
    """Resuelve E.164 desde JID de WhatsApp (incluye remoteJidAlt para @lid)."""
    if remote_jid.endswith("@lid"):
        return ""

    if key:
        alt = key.get("remoteJidAlt") or key.get("participant")
        if isinstance(alt, str) and alt.endswith("@s.whatsapp.net"):
            return jid_to_phone(alt)
        main = key.get("remoteJid")
        if isinstance(main, str) and main.endswith("@s.whatsapp.net"):
            return jid_to_phone(main)

    if remote_jid.endswith("@s.whatsapp.net"):
        return jid_to_phone(remote_jid)

    return ""

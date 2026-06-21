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

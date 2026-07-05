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


def is_untrusted_contact_phone(phone_e164: str) -> bool:
    """Números de prueba o placeholders incorrectos (p. ej. 3001234567)."""
    if not phone_e164 or is_lid_placeholder(phone_e164):
        return False
    digits = re.sub(r"\D", "", phone_e164)
    local = digits[-10:] if len(digits) >= 10 else digits
    if not local:
        return False
    if local.startswith("300123456"):
        return True
    if local in {"3000000000", "3001111111", "1234567890", "0123456789"}:
        return True
    return False


def phone_trust_rank(phone_e164: str) -> int:
    """Mayor = más confiable para elegir identidad canónica."""
    if not phone_e164 or is_lid_placeholder(phone_e164):
        return 0
    if not is_valid_whatsapp_phone(phone_e164):
        return 0
    if is_untrusted_contact_phone(phone_e164):
        return 1
    return 2


def is_lid_placeholder(phone: str) -> bool:
    return bool(phone) and phone.startswith("lid:")


def lid_digits_from_jid(contact_jid: str) -> str:
    jid = (contact_jid or "").strip()
    if not jid.endswith("@lid"):
        return ""
    return jid.split("@")[0].split(":")[0]


def phone_matches_lid_digits(phone: str, contact_jid: str = "") -> bool:
    """True si el teléfono es solo los dígitos del @lid (no un número WA real)."""
    lid_digits = lid_digits_from_jid(contact_jid)
    if not lid_digits:
        return False
    phone_digits = re.sub(r"\D", "", phone or "")
    return bool(phone_digits) and phone_digits == lid_digits


def is_lid_derived_phone(phone: str, contact_jid: str = "") -> bool:
    """Teléfono inferido desde @lid o placeholder lid:… — no usar para sendText por número."""
    if is_lid_placeholder(phone):
        return True
    return phone_matches_lid_digits(phone, contact_jid)


def is_placeholder_contact_name(name: str, phone: str) -> bool:
    if not name:
        return True
    if str(name).strip().lower() in {"contacto", "contact", "unknown", "desconocido"}:
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


def lid_short_ref(phone: str, *, contact_jid: str = "") -> str:
    """Últimos dígitos del @lid para identificar contactos sin número visible."""
    lid_id = ""
    if contact_jid and contact_jid.endswith("@lid"):
        lid_id = contact_jid.split("@")[0]
    elif is_lid_placeholder(phone):
        lid_id = phone[4:]
    if not lid_id:
        return ""
    tail = lid_id[-5:] if len(lid_id) >= 5 else lid_id
    return f"····{tail}"


def format_contact_display_phone(
    phone: str,
    *,
    contact_jid: str = "",
    linked_phone: str = "",
) -> str:
    """Teléfono legible o referencia corta cuando WhatsApp oculta el número (@lid)."""
    candidate = (linked_phone or "").strip()
    if not candidate or is_lid_placeholder(candidate):
        if phone and not is_lid_placeholder(phone):
            normalized = normalize_phone(phone)
            if is_valid_whatsapp_phone(normalized):
                candidate = normalized
    if candidate:
        normalized = normalize_phone(candidate)
        if is_valid_whatsapp_phone(normalized):
            return normalized
    ref = lid_short_ref(phone, contact_jid=contact_jid)
    if ref:
        return f"Sin número · ref {ref}"
    return ""


def resolve_display_name(name: str, phone: str, *, contact_jid: str = "") -> str:
    if name and not is_placeholder_contact_name(name, phone):
        return name
    display_phone = format_display_phone(phone)
    if display_phone:
        return display_phone
    if contact_jid and contact_jid.endswith("@lid"):
        lid_id = contact_jid.split("@")[0]
        if len(lid_id) > 6:
            return f"···{lid_id[-6:]}"
        return f"···{lid_id}" if lid_id else "Chat"
    if is_lid_placeholder(phone):
        lid_id = phone[4:]
        if len(lid_id) > 6:
            return f"···{lid_id[-6:]}"
        return f"···{lid_id}" if lid_id else "Chat"
    return "Chat"


def _normalize_person_name(name: str) -> str:
    return re.sub(r"[^\w\s]", "", (name or "").strip().lower())


def names_likely_same_person(a: str, b: str) -> bool:
    """Solo coincidencia exacta (sin subcadenas: 'ana' ⊂ 'diana' causaba merges cruzados)."""
    na = _normalize_person_name(a)
    nb = _normalize_person_name(b)
    if not na or not nb:
        return False
    return na == nb


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


def pair_lid_phone_from_message_key(key: Optional[dict]) -> tuple[str, str]:
    """Enlace @lid↔teléfono solo si WhatsApp los envía juntos en el mismo message key."""
    if not key or not isinstance(key, dict):
        return "", ""
    lid = ""
    phone = ""
    for field in ("remoteJid", "remoteJidAlt"):
        val = str(key.get(field) or "").strip()
        if val.endswith("@lid"):
            lid = val
        elif val.endswith("@s.whatsapp.net"):
            candidate = jid_to_phone(val)
            if is_valid_whatsapp_phone(candidate):
                phone = normalize_phone(candidate)
    if lid and phone:
        return lid, phone
    return "", ""


def phone_match_tail(a: str, b: str, tail_len: int = 10) -> bool:
    """Compara los últimos N dígitos para tolerar +57 vs 57 vs 10 dígitos locales."""
    da = re.sub(r"\D", "", a or "")
    db = re.sub(r"\D", "", b or "")
    if not da or not db:
        return False
    ta = da[-tail_len:] if len(da) >= tail_len else da
    tb = db[-tail_len:] if len(db) >= tail_len else db
    return ta == tb and len(ta) >= tail_len


def lid_jid_from_lid_phone(phone: str) -> str:
    if not is_lid_placeholder(phone):
        return ""
    lid = phone[4:]
    return f"{lid}@lid" if lid and "@" not in lid else lid


def evolution_send_target(conversation) -> str:
    """Número o JID para Evolution sendText."""
    jid = getattr(conversation, "contact_jid", None) or ""
    if jid and "@" in jid:
        return jid
    phone = conversation.contact_phone or ""
    if is_lid_derived_phone(phone, jid):
        lid_digits = phone[4:] if is_lid_placeholder(phone) else lid_digits_from_jid(jid)
        if lid_digits:
            return f"{lid_digits}@lid"
    if phone.startswith("lid:"):
        lid = phone[4:]
        return f"{lid}@lid" if "@" not in lid else lid
    return phone_to_evolution_number(phone)


def resolve_contact_phone(remote_jid: str, *, key: Optional[dict] = None) -> str:
    """Resuelve E.164 desde JID de WhatsApp (incluye remoteJidAlt para @lid)."""
    candidates = collect_phone_candidates(remote_jid, key=key)
    if remote_jid.endswith("@lid"):
        lid_digits = remote_jid.split("@")[0].split(":")[0]
        candidates = [
            p for p in candidates if re.sub(r"\D", "", p or "") != lid_digits
        ]
    return pick_trusted_phone(candidates)


def collect_phone_candidates(remote_jid: str, *, key: Optional[dict] = None) -> list[str]:
    """Todos los teléfonos plausibles en el mensaje (sin placeholders)."""
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str) -> None:
        if not raw:
            return
        if raw.endswith("@s.whatsapp.net"):
            phone = jid_to_phone(raw)
        else:
            digits = re.sub(r"\D", "", raw)
            if len(digits) < 10:
                return
            phone = normalize_phone(digits)
        if not phone or not is_valid_whatsapp_phone(phone):
            return
        norm = normalize_phone(phone)
        if norm in seen:
            return
        seen.add(norm)
        out.append(norm)

    if remote_jid.endswith("@s.whatsapp.net"):
        add(remote_jid)

    if key:
        for field in (
            "remoteJid",
            "remoteJidAlt",
            "participant",
            "senderPn",
            "participantPn",
            "participantAlt",
        ):
            alt = key.get(field)
            if isinstance(alt, str):
                add(alt)

    return out


def pick_trusted_phone(candidates: list[str]) -> str:
    if not candidates:
        return ""
    trusted = [p for p in candidates if not is_untrusted_contact_phone(p)]
    if trusted:
        return normalize_phone(trusted[0])
    return normalize_phone(candidates[0])

from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import Conversation, WhatsAppContactLink, WhatsAppSession
from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client
from app.shared.core.phone import (
    is_group_or_broadcast_jid,
    is_lid_derived_phone,
    is_untrusted_contact_phone,
    is_valid_whatsapp_phone,
    jid_to_phone,
    normalize_phone,
)

log = logging.getLogger(__name__)


def _contact_name(row: dict) -> str:
    return str(
        row.get("name")
        or row.get("verifiedName")
        or row.get("pushName")
        or row.get("notify")
        or ""
    ).strip()


def _contact_identifiers(row: dict) -> list[str]:
    values: list[str] = []
    for field in ("remoteJid", "id", "jid", "phoneJid"):
        value = str(row.get(field) or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def _safe_phone(phone: str, *, jid: str = "") -> str:
    normalized = normalize_phone(phone)
    if (
        not normalized
        or not is_valid_whatsapp_phone(normalized)
        or is_untrusted_contact_phone(normalized)
        or is_lid_derived_phone(normalized, jid)
    ):
        return ""
    return normalized


def _add_contact(
    contacts: dict[str, dict],
    *,
    phone: str,
    name: str,
    source: str,
) -> None:
    normalized = _safe_phone(phone)
    if not normalized:
        return
    current = contacts.get(normalized)
    if current is None:
        contacts[normalized] = {
            "phone_e164": normalized,
            "name": name or normalized,
            "source": source,
            "verified": True,
        }
        return
    if name and (not current["name"] or current["name"] == normalized):
        current["name"] = name
    sources = set(str(current["source"]).split("+"))
    sources.add(source)
    current["source"] = "+".join(sorted(sources))


def list_safe_whatsapp_contacts(
    db: Session,
    *,
    session: WhatsAppSession,
    limit: int = 500,
) -> dict:
    """Lista contactos aptos para difusión sin exponer ni enviar a @lid ambiguos."""
    if session.active_connection_id is None:
        return {"contacts": [], "omitted_ambiguous": 0}

    connection_id = session.active_connection_id
    links = (
        db.query(WhatsAppContactLink)
        .filter(
            WhatsAppContactLink.tenant_id == session.tenant_id,
            WhatsAppContactLink.whatsapp_connection_id == connection_id,
        )
        .all()
    )
    lid_to_phone = {
        row.lid_jid: normalize_phone(row.phone_e164)
        for row in links
        if _safe_phone(row.phone_e164, jid=row.lid_jid)
    }

    if settings.evolution_database_url:
        from app.infrastructure.evolution.evolution_store import (
            fetch_bidirectional_lid_mappings,
        )

        evolution_lids, _ = fetch_bidirectional_lid_mappings(
            settings.evolution_database_url,
            session.instance_name,
        )
        for lid, phone in evolution_lids.items():
            if lid not in lid_to_phone and _safe_phone(phone, jid=lid):
                lid_to_phone[lid] = normalize_phone(phone)

    rows: list[dict] = []
    try:
        rows.extend(evolution_client.find_contacts(session.instance_name))
    except EvolutionAPIError as exc:
        log.warning("findContacts no disponible instancia=%s: %s", session.instance_name, exc)

    if settings.evolution_database_url:
        from app.infrastructure.evolution.evolution_store import fetch_stored_contacts

        rows.extend(
            fetch_stored_contacts(
                settings.evolution_database_url,
                session.instance_name,
                limit=max(limit * 4, 2000),
            )
        )

    contacts: dict[str, dict] = {}
    ambiguous = 0
    owner_phone = normalize_phone(session.phone_number or "")

    for row in rows:
        if not isinstance(row, dict):
            continue
        identifiers = _contact_identifiers(row)
        if not identifiers or any(is_group_or_broadcast_jid(jid) for jid in identifiers):
            continue
        phone = ""
        direct_jid = next(
            (jid for jid in identifiers if jid.endswith("@s.whatsapp.net")),
            "",
        )
        lid_jid = next((jid for jid in identifiers if jid.endswith("@lid")), "")
        if direct_jid:
            phone = _safe_phone(jid_to_phone(direct_jid), jid=direct_jid)
        else:
            for field in ("phone", "number", "phoneNumber"):
                phone = _safe_phone(str(row.get(field) or ""))
                if phone:
                    break
        if not phone and lid_jid:
            phone = _safe_phone(lid_to_phone.get(lid_jid, ""), jid=lid_jid)
            if not phone:
                ambiguous += 1
                continue
        if not phone or phone == owner_phone:
            continue
        _add_contact(
            contacts,
            phone=phone,
            name=_contact_name(row),
            source="whatsapp",
        )

    conversations: Iterable[Conversation] = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == session.tenant_id,
            Conversation.whatsapp_connection_id == connection_id,
        )
        .all()
    )
    for conversation in conversations:
        phone = _safe_phone(
            conversation.contact_phone,
            jid=conversation.contact_jid or "",
        )
        if not phone or phone == owner_phone:
            continue
        _add_contact(
            contacts,
            phone=phone,
            name=conversation.contact_name or "",
            source="chat",
        )

    ordered = sorted(
        contacts.values(),
        key=lambda item: (str(item["name"]).lower(), item["phone_e164"]),
    )
    return {
        "contacts": ordered[: max(1, min(limit, 500))],
        "omitted_ambiguous": ambiguous,
    }


def validate_selected_contact_phones(
    db: Session,
    *,
    session: WhatsAppSession,
    phones: list[str],
) -> tuple[list[dict], list[str]]:
    directory = list_safe_whatsapp_contacts(db, session=session, limit=500)
    by_phone = {
        row["phone_e164"]: row
        for row in directory["contacts"]
    }
    selected: list[dict] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for raw in phones:
        phone = normalize_phone(raw)
        if phone in seen:
            continue
        seen.add(phone)
        row = by_phone.get(phone)
        if row is None:
            rejected.append(raw)
            continue
        selected.append({"phone": phone, "name": row["name"]})
    return selected, rejected

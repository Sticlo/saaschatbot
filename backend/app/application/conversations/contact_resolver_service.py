from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.domain.entities import Conversation, WhatsAppContactLink
from app.shared.core.phone import (
    is_lid_placeholder,
    is_placeholder_contact_name,
    is_untrusted_contact_phone,
    is_valid_whatsapp_phone,
    jid_to_phone,
    lid_jid_from_lid_phone,
    normalize_phone,
    pair_lid_phone_from_message_key,
    phone_to_evolution_number,
    phone_trust_rank,
    pick_trusted_phone,
    collect_phone_candidates,
    resolve_contact_phone,
)

log = logging.getLogger(__name__)


def extract_message_identities(
    remote_jid: str,
    *,
    lid_jid: str = "",
    message_key: Optional[dict] = None,
) -> tuple[str, str]:
    """Devuelve (teléfono E.164 o lid:…, jid @lid si existe)."""
    lid = lid_jid or (remote_jid if remote_jid.endswith("@lid") else "")
    if not lid and message_key:
        alt = str(message_key.get("remoteJidAlt") or "")
        main = str(message_key.get("remoteJid") or "")
        if alt.endswith("@lid"):
            lid = alt
        elif main.endswith("@lid"):
            lid = main

    phone = pick_trusted_phone(collect_phone_candidates(remote_jid, key=message_key))
    if not phone:
        phone = resolve_contact_phone(remote_jid, key=message_key)
    if not phone and lid:
        phone = f"lid:{lid.split('@')[0]}"
    if phone and is_valid_whatsapp_phone(phone):
        phone = normalize_phone(phone)
    return phone, lid


def record_contact_link(
    db: Session,
    *,
    tenant_id: UUID,
    whatsapp_connection_id: UUID,
    lid_jid: str,
    phone_e164: str,
    verified: bool = False,
) -> None:
    """Persiste o actualiza el puente @lid ↔ teléfono (solo verified=True desde message key)."""
    if not lid_jid.endswith("@lid") or not is_valid_whatsapp_phone(phone_e164):
        return
    if is_untrusted_contact_phone(phone_e164):
        return

    phone_e164 = normalize_phone(phone_e164)

    def _find_by_lid() -> Optional[WhatsAppContactLink]:
        rows = (
            db.query(WhatsAppContactLink)
            .filter(
                WhatsAppContactLink.tenant_id == tenant_id,
                WhatsAppContactLink.lid_jid == lid_jid,
            )
            .all()
        )
        for row in rows:
            if row.whatsapp_connection_id == whatsapp_connection_id:
                return row
        return rows[0] if rows else None

    def _find_by_phone() -> Optional[WhatsAppContactLink]:
        rows = (
            db.query(WhatsAppContactLink)
            .filter(
                WhatsAppContactLink.tenant_id == tenant_id,
                WhatsAppContactLink.phone_e164 == phone_e164,
            )
            .all()
        )
        for row in rows:
            if row.whatsapp_connection_id == whatsapp_connection_id:
                return row
        return rows[0] if rows else None

    existing_lid = _find_by_lid()
    existing_phone = _find_by_phone()

    if existing_lid and existing_lid.phone_e164 != phone_e164:
        if not verified:
            log.debug(
                "Enlace @lid ignorado (sin key verificado) tenant=%s %s ≠ %s",
                tenant_id,
                lid_jid,
                phone_e164,
            )
            return
        if existing_phone and existing_phone.id != existing_lid.id:
            log.info(
                "Enlace @lid actualizado tenant=%s %s → %s",
                tenant_id,
                lid_jid,
                phone_e164,
            )
            db.delete(existing_phone)
        existing_lid.phone_e164 = phone_e164
        existing_lid.whatsapp_connection_id = whatsapp_connection_id
        return

    if existing_phone and existing_phone.lid_jid != lid_jid:
        if not verified:
            log.debug(
                "Teléfono ya vinculado a otro @lid tenant=%s %s (omitido)",
                tenant_id,
                phone_e164,
            )
            return
        if existing_lid and existing_lid.id != existing_phone.id:
            db.delete(existing_lid)
        existing_phone.lid_jid = lid_jid
        existing_phone.whatsapp_connection_id = whatsapp_connection_id
        return

    if existing_lid:
        existing_lid.whatsapp_connection_id = whatsapp_connection_id
        return
    if existing_phone:
        existing_phone.whatsapp_connection_id = whatsapp_connection_id
        return

    db.add(
        WhatsAppContactLink(
            tenant_id=tenant_id,
            whatsapp_connection_id=whatsapp_connection_id,
            lid_jid=lid_jid,
            phone_e164=phone_e164,
        )
    )
    db.flush()


def _load_link_maps(
    db: Session,
    *,
    tenant_id: UUID,
    whatsapp_connection_id: UUID,
) -> tuple[dict[str, str], dict[str, str]]:
    lid_to_phone: dict[str, str] = {}
    phone_to_lid: dict[str, str] = {}
    rows = (
        db.query(WhatsAppContactLink)
        .filter(WhatsAppContactLink.tenant_id == tenant_id)
        .all()
    )
    # Priorizar enlaces de la vinculación actual; fallback a enlaces de QR anteriores.
    rows.sort(
        key=lambda r: (
            0 if r.whatsapp_connection_id == whatsapp_connection_id else 1,
            str(r.updated_at or r.created_at or ""),
        )
    )
    for row in rows:
        if is_untrusted_contact_phone(row.phone_e164):
            continue
        if row.lid_jid not in lid_to_phone:
            lid_to_phone[row.lid_jid] = row.phone_e164
        digits = phone_to_evolution_number(row.phone_e164)
        if row.phone_e164 not in phone_to_lid:
            phone_to_lid[row.phone_e164] = row.lid_jid
        if digits not in phone_to_lid:
            phone_to_lid[digits] = row.lid_jid
    return lid_to_phone, phone_to_lid


def _collect_candidates(
    db: Session,
    *,
    tenant_id: UUID,
    whatsapp_connection_id: UUID,
    phone: str,
    lid_jid: str,
    instance_name: str,
    fast: bool = True,
    push_name: str = "",
) -> list[Conversation]:
    from app.config import settings
    from app.infrastructure.evolution.evolution_store import fetch_lid_jid_for_phone

    jids_to_try: list[str] = []
    phones_to_try: list[str] = []

    if lid_jid:
        jids_to_try.append(lid_jid)
    if phone:
        phones_to_try.append(phone)
    if is_lid_placeholder(phone):
        derived = lid_jid_from_lid_phone(phone)
        if derived and derived not in jids_to_try:
            jids_to_try.append(derived)
    if is_valid_whatsapp_phone(phone):
        norm = normalize_phone(phone)
        if norm not in phones_to_try:
            phones_to_try.append(norm)

    lid_to_phone, phone_to_lid = _load_link_maps(
        db,
        tenant_id=tenant_id,
        whatsapp_connection_id=whatsapp_connection_id,
    )

    if not fast and instance_name and settings.evolution_database_url:
        from app.infrastructure.evolution.evolution_store import (
            fetch_bidirectional_lid_mappings,
        )

        evo_lid, evo_phone = fetch_bidirectional_lid_mappings(
            settings.evolution_database_url, instance_name
        )
        lid_to_phone.update(evo_lid)
        phone_to_lid.update(evo_phone)

    for p in list(phones_to_try):
        if not is_valid_whatsapp_phone(p):
            continue
        norm = normalize_phone(p)
        lid = phone_to_lid.get(norm) or phone_to_lid.get(phone_to_evolution_number(norm))
        if (
            not lid
            and not fast
            and settings.evolution_database_url
            and instance_name
        ):
            lid = fetch_lid_jid_for_phone(
                settings.evolution_database_url, instance_name, norm
            ) or ""
        if lid and lid not in jids_to_try:
            jids_to_try.append(lid)

    for jid in list(jids_to_try):
        if jid.endswith("@lid"):
            mapped = lid_to_phone.get(jid)
            if mapped and is_valid_whatsapp_phone(mapped) and not is_untrusted_contact_phone(mapped):
                norm = normalize_phone(mapped)
                if norm not in phones_to_try:
                    phones_to_try.append(norm)

    found: dict[UUID, Conversation] = {}

    def _add(conv: Optional[Conversation]) -> None:
        if conv is not None:
            found[conv.id] = conv

    base = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.whatsapp_connection_id == whatsapp_connection_id,
        )
    )

    for jid in jids_to_try:
        _add(base.filter(Conversation.contact_jid == jid).first())

    for p in phones_to_try:
        _add(base.filter(Conversation.contact_phone == p).first())

    for jid in list(jids_to_try):
        if not jid.endswith("@lid"):
            continue
        lid_key = jid.split("@")[0]
        for conv in base.filter(Conversation.contact_phone == f"lid:{lid_key}").limit(4):
            _add(conv)

    return list(found.values())


def _merge_to_canonical(
    db: Session,
    *,
    tenant_id: UUID,
    candidates: list[Conversation],
    owner_names: Optional[set[str]] = None,
) -> Conversation:
    from app.application.conversations.whatsapp_conversation_service import (
        merge_conversations,
        pick_merge_primary,
    )

    owner_names = owner_names or set()
    primary = candidates[0]
    for other in candidates[1:]:
        if other.id == primary.id:
            continue
        primary, secondary = pick_merge_primary(primary, other, owner_names=owner_names)
        merge_conversations(
            db,
            tenant_id=tenant_id,
            primary=primary,
            secondary=secondary,
            owner_names=owner_names,
        )
    return primary


def _pick_exact_conversation(
    candidates: list[Conversation],
    *,
    remote_jid: str,
    phone: str,
    lid_jid: str,
) -> Optional[Conversation]:
    """Elige UN chat — nunca fusiona. Prioridad: jid exacto → teléfono exacto."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    phone_targets: set[str] = set()
    for raw in (phone, jid_to_phone(remote_jid) if remote_jid else ""):
        if raw and is_valid_whatsapp_phone(raw):
            phone_targets.add(normalize_phone(raw))

    if phone_targets and (remote_jid.endswith("@s.whatsapp.net") or any(
        is_valid_whatsapp_phone(p) for p in (phone,)
    )):
        for conv in candidates:
            if is_valid_whatsapp_phone(conv.contact_phone):
                if normalize_phone(conv.contact_phone) in phone_targets:
                    return conv

    jid_targets = {j for j in (remote_jid, lid_jid) if j and j.endswith("@lid")}
    for conv in candidates:
        if conv.contact_jid and conv.contact_jid in jid_targets:
            return conv

    for conv in candidates:
        if is_valid_whatsapp_phone(conv.contact_phone):
            if normalize_phone(conv.contact_phone) in phone_targets:
                return conv

    # Varios candidatos sin match exacto: no adivinar (evita Ana↔Diana).
    log.warning(
        "Conversación ambigua — %s candidatos jid=%s phone=%s (sin fusión)",
        len(candidates),
        remote_jid or lid_jid,
        phone,
    )
    return None


def resolve_canonical_conversation(
    db: Session,
    *,
    tenant_id: UUID,
    whatsapp_connection_id: UUID,
    remote_jid: str = "",
    lid_jid: str = "",
    message_key: Optional[dict] = None,
    instance_name: str = "",
    owner_names: Optional[set[str]] = None,
    push_name: str = "",
) -> tuple[Optional[Conversation], str, str]:
    """Un solo chat por persona: fusiona duplicados antes de guardar el mensaje."""
    phone, lid = extract_message_identities(
        remote_jid, lid_jid=lid_jid, message_key=message_key
    )

    verified_lid, verified_phone = pair_lid_phone_from_message_key(message_key)
    if verified_lid and verified_phone:
        record_contact_link(
            db,
            tenant_id=tenant_id,
            whatsapp_connection_id=whatsapp_connection_id,
            lid_jid=verified_lid,
            phone_e164=verified_phone,
            verified=True,
        )

    candidates = _collect_candidates(
        db,
        tenant_id=tenant_id,
        whatsapp_connection_id=whatsapp_connection_id,
        phone=phone,
        lid_jid=lid,
        instance_name=instance_name,
        push_name=push_name,
    )

    conv = _pick_exact_conversation(
        candidates,
        remote_jid=remote_jid,
        phone=phone,
        lid_jid=lid,
    )

    if conv is not None:
        from app.application.sync.contact_identity_service import apply_identity_to_conversation

        trusted_phone = phone if is_valid_whatsapp_phone(phone) and not is_untrusted_contact_phone(phone) else ""
        if trusted_phone and is_lid_placeholder(conv.contact_phone):
            trusted_phone = ""
        apply_identity_to_conversation(
            conv,
            contact_phone=trusted_phone or conv.contact_phone,
            contact_name="",
            contact_jid=lid or conv.contact_jid or "",
        )
    else:
        return None, phone, lid

    return conv, phone, lid


def repair_duplicates_for_contact(
    db: Session,
    *,
    tenant_id: UUID,
    whatsapp_connection_id: UUID,
    phone: str,
    lid_jid: str = "",
    instance_name: str = "",
    owner_names: Optional[set[str]] = None,
    push_name: str = "",
) -> Optional[Conversation]:
    """Sin fusión automática — devuelve match exacto si existe."""
    from app.application.chatwoot.chatwoot_service import chatwoot_sync_mode

    if chatwoot_sync_mode():
        return None
    candidates = _collect_candidates(
        db,
        tenant_id=tenant_id,
        whatsapp_connection_id=whatsapp_connection_id,
        phone=phone,
        lid_jid=lid_jid,
        instance_name=instance_name,
        push_name=push_name,
    )
    if not candidates:
        return None
    return _pick_exact_conversation(
        candidates,
        remote_jid=lid_jid,
        phone=phone,
        lid_jid=lid_jid,
    )


def learn_link_from_key(
    db: Session,
    *,
    tenant_id: UUID,
    whatsapp_connection_id: UUID,
    message_key: Optional[dict],
    remote_jid: str = "",
    lid_jid: str = "",
) -> None:
    """Aprende @lid↔teléfono solo cuando WhatsApp los envía juntos en el message key."""
    _ = remote_jid, lid_jid
    verified_lid, verified_phone = pair_lid_phone_from_message_key(message_key)
    if verified_lid and verified_phone:
        record_contact_link(
            db,
            tenant_id=tenant_id,
            whatsapp_connection_id=whatsapp_connection_id,
            lid_jid=verified_lid,
            phone_e164=verified_phone,
            verified=True,
        )

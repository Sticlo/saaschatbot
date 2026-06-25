from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.shared.core.phone import (
    is_group_or_broadcast_jid,
    is_owner_jid,
    is_valid_whatsapp_phone,
    normalize_phone,
    phone_to_evolution_number,
    resolve_contact_phone,
)
from app.domain.entities import (
    Conversation,
    Message,
    MessageDirection,
    MessageSource,
    MessageStatus,
    Tenant,
    WhatsAppSession,
    WhatsAppStatus,
)
from app.application.sync.contact_identity_service import (
    apply_identity_to_conversation,
    build_contacts_index,
    enrich_tenant_conversations,
    resolve_contact_identity,
)
from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client
from app.infrastructure.evolution.evolution_store import (
    connection_since_unix,
    fetch_chat_last_timestamp,
    fetch_contact_names_index,
    fetch_contact_push_name,
    fetch_lid_phone_mappings,
    fetch_message_chat_index,
    fetch_stored_chats,
    fetch_stored_contacts,
    fetch_stored_counts,
    fetch_stored_messages,
)
from app.application.messaging.message_service import _extract_message_body, get_or_create_conversation, _find_existing_message
from app.application.realtime.realtime_service import publish_conversation_updated
from app.application.whatsapp.whatsapp_service import refresh_session_status

log = logging.getLogger(__name__)

_HISTORY_POLL_ATTEMPTS = 18
_HISTORY_POLL_SECONDS = 5
_MAX_CHATS_PER_SYNC = 1000
_BULK_MESSAGE_PAGES = 20
_BULK_MESSAGE_PAGE_SIZE = 500


def _fetch_chat_states(instance_name: str) -> dict[str, dict]:
    try:
        rows = evolution_client.fetch_chat_states(instance_name)
    except EvolutionAPIError as exc:
        log.warning("fetch_chat_states falló: %s", exc)
        return {}
    states: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        jid = str(row.get("remoteJid") or "")
        if jid:
            states[jid] = row
    return states


def _owner_display_names(session: WhatsAppSession, *, dsn: str, instance_name: str) -> set[str]:
    names: set[str] = set()
    if session.bound_owner_jid and dsn:
        push = fetch_contact_push_name(dsn, instance_name, session.bound_owner_jid)
        if push:
            names.add(push)
    return names


def _contact_identity(
    remote_jid: str,
    item: dict,
    *,
    contacts_index: dict[str, dict],
    instance_name: str,
    dsn: str,
    key: Optional[dict] = None,
) -> Optional[tuple[str, str, str, Optional[bool]]]:
    if is_group_or_broadcast_jid(remote_jid):
        return None
    return resolve_contact_identity(
        remote_jid,
        item,
        contacts_index=contacts_index,
        instance_name=instance_name,
        dsn=dsn,
        key=key,
        lid_jid=str(item.get("_lid_jid") or ""),
    )


def _parse_evolution_message(
    record: dict,
    *,
    expected_phone: str,
    expected_jid: str = "",
) -> Optional[dict]:
    key = record.get("key") or {}
    if not isinstance(key, dict):
        return None
    msg_id = key.get("id")
    body = _extract_message_body(record.get("message") or {})
    if not msg_id or not body.strip():
        return None
    # Truncar cuerpo para evitar StringDataRightTruncation
    body = body[:32000]

    remote_jid = key.get("remoteJid") or key.get("remoteJidAlt") or ""
    phone = resolve_contact_phone(str(remote_jid), key=key)
    lid_jid = str(remote_jid) if str(remote_jid).endswith("@lid") else ""
    if lid_jid and not phone:
        phone = f"lid:{lid_jid.split('@')[0]}"
    if not phone:
        return None

    if phone != expected_phone:
        if not (expected_jid and str(remote_jid) == expected_jid):
            return None

    ts = record.get("messageTimestamp")
    created_at = None
    if ts:
        try:
            created_at = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (TypeError, ValueError):
            created_at = None

    return {
        "message_id": str(msg_id),
        "phone": phone,
        "contact_name": record.get("pushName") or "",
        "body": body.strip(),
        "from_me": bool(key.get("fromMe")),
        "created_at": created_at,
    }


def _nudge_history_sync(instance_name: str, *, tenant_id) -> dict[str, int]:
    """Activa webhook de historial, syncFullHistory y reinicia Evolution."""
    from app.config import settings

    webhook_url = f"{settings.app_public_url.rstrip('/')}/webhooks/evolution/{tenant_id}"
    dsn = settings.evolution_database_url
    baseline = fetch_stored_counts(dsn, instance_name)

    try:
        evolution_client.ensure_webhook(
            instance_name,
            webhook_url,
            settings.evolution_webhook_secret,
        )
    except EvolutionAPIError as exc:
        log.warning("ensure_webhook: %s", exc)

    try:
        evolution_client.set_settings(
            instance_name,
            {
                "rejectCall": False,
                "groupsIgnore": True,
                "alwaysOnline": False,
                "readMessages": False,
                "readStatus": False,
                "syncFullHistory": True,
            },
            timeout=8.0,
        )
    except EvolutionAPIError as exc:
        log.warning("syncFullHistory: %s", exc)

    already_open = False
    try:
        state_payload = evolution_client.connection_state(instance_name)
        state = str((state_payload.get("instance") or {}).get("state") or "").lower()
        already_open = state in {"open", "connected"}
    except EvolutionAPIError as exc:
        log.warning("connection_state: %s", exc)

    if already_open:
        log.info("Instancia %s ya conectada — sin reiniciar para pedir historial", instance_name)
        return baseline

    try:
        evolution_client.restart_instance(instance_name)
        log.info("Evolution reiniciado para pedir historial del celular")
    except EvolutionAPIError as exc:
        log.warning("restart_instance: %s — intentando connect", exc)
        try:
            evolution_client.connect_instance(instance_name)
        except EvolutionAPIError as exc2:
            log.warning("connect nudge: %s", exc2)

    return baseline


def _collect_evolution_items(
    instance_name: str,
    *,
    tenant_id,
    wait_for_history: bool,
    since_ts: Optional[int] = None,
) -> tuple[list, list, list, list, list]:
    dsn = settings.evolution_database_url
    chats: list = []
    contacts: list = []
    stored_chats: list = []
    stored_contacts: list = []
    message_index: list = []
    attempts = _HISTORY_POLL_ATTEMPTS if wait_for_history else 1
    baseline = {"chats": 0, "contacts": 0, "messages": 0}
    stable_polls = 0
    prev_total = 0

    if wait_for_history:
        baseline = _nudge_history_sync(instance_name, tenant_id=tenant_id)
        time.sleep(8)

    for attempt in range(attempts):
        try:
            chats = evolution_client.find_chats(instance_name)
        except EvolutionAPIError as exc:
            log.warning("find_chats falló: %s", exc)
            chats = []
        try:
            contacts = evolution_client.find_contacts(instance_name)
        except EvolutionAPIError as exc:
            log.warning("find_contacts falló: %s", exc)
            contacts = []

        stored_chats = fetch_stored_chats(dsn, instance_name, limit=_MAX_CHATS_PER_SYNC)
        stored_contacts = fetch_stored_contacts(dsn, instance_name, limit=_MAX_CHATS_PER_SYNC)
        message_index = fetch_message_chat_index(
            dsn, instance_name, limit=_MAX_CHATS_PER_SYNC, since_ts=None
        )
        counts = fetch_stored_counts(dsn, instance_name)

        total = (
            len(chats)
            + len(contacts)
            + len(stored_chats)
            + len(stored_contacts)
            + len(message_index)
        )
        db_total = counts["chats"] + counts["contacts"] + counts["messages"]
        grew = db_total > sum(baseline.values())

        if total > 0 and (not wait_for_history or grew):
            if total == prev_total:
                stable_polls += 1
            else:
                stable_polls = 0
            prev_total = total
            if stable_polls >= 2 or attempt == attempts - 1:
                log.info(
                    "Historial listo — chats=%s contacts=%s msg_jids=%s db_msgs=%s",
                    len(stored_chats),
                    len(stored_contacts),
                    len(message_index),
                    counts["messages"],
                )
                break

        if not wait_for_history or attempt == attempts - 1:
            break

        log.info(
            "Esperando historial del celular (%s/%s) db_msgs=%s…",
            attempt + 1,
            attempts,
            counts["messages"],
        )
        time.sleep(_HISTORY_POLL_SECONDS)

    return chats, contacts, stored_chats, stored_contacts, message_index


def _discover_jids_from_api(instance_name: str, *, since_ts: Optional[int] = None) -> list[dict]:
    """Descubre chats escaneando mensajes paginados en Evolution (sin filtrar por vinculación)."""
    del since_ts
    seen: set[str] = set()
    items: list[dict] = []
    for page in range(1, _BULK_MESSAGE_PAGES + 1):
        try:
            payload = evolution_client.find_recent_messages(
                instance_name,
                limit=_BULK_MESSAGE_PAGE_SIZE,
                page=page,
            )
        except EvolutionAPIError:
            break
        records = (payload.get("messages") or {}).get("records") or []
        if not records:
            break
        for record in records:
            key = record.get("key") or {}
            jid = str(key.get("remoteJid") or "")
            if not jid or jid in seen or is_group_or_broadcast_jid(jid):
                continue
            seen.add(jid)
            item: dict = {"remoteJid": jid, "pushName": record.get("pushName") or ""}
            ts = record.get("messageTimestamp")
            if ts is not None:
                try:
                    item["lastMessageTimestamp"] = int(ts)
                except (TypeError, ValueError):
                    pass
            items.append(item)
        pages = (payload.get("messages") or {}).get("pages") or 1
        if page >= pages:
            break
    return items


def _merge_item_fields(target: dict, source: dict) -> None:
    for key in ("name", "pushName", "verifiedName", "remoteJid"):
        val = source.get(key)
        if val and (not target.get(key) or key == "name"):
            target[key] = val
    src_ts = source.get("lastMessageTimestamp")
    if src_ts is not None:
        try:
            prev = int(target.get("lastMessageTimestamp") or 0)
            target["lastMessageTimestamp"] = max(prev, int(src_ts))
        except (TypeError, ValueError):
            target["lastMessageTimestamp"] = src_ts
    if source.get("archived") is not None:
        target["archived"] = bool(source.get("archived"))


def _merge_items(
    chats: list,
    contacts: list,
    stored_chats: list,
    stored_contacts: list,
    message_index: list,
    api_discovered: list | None = None,
    chat_states: dict | None = None,
) -> list[tuple[str, dict]]:
    by_jid: dict[str, dict] = {}
    chat_states = chat_states or {}

    # Orden de prioridad: primero las fuentes que indican un chat REAL con mensajes
    # (message_index, stored_chats, chats, api_discovered). Los contactos van al
    # final y solo se usan para enriquecer datos de chats ya existentes — así no
    # ocupan los cupos de _MAX_CHATS_PER_SYNC desplazando chats con historial.
    primary_sources = (
        message_index,
        stored_chats,
        chats,
        api_discovered or [],
    )
    enrichment_sources = (
        stored_contacts,
        contacts,
    )

    for source in primary_sources:
        for item in source:
            if not isinstance(item, dict):
                continue
            jid = str(item.get("remoteJid") or "")
            if not jid or is_group_or_broadcast_jid(jid):
                continue
            if jid in by_jid:
                _merge_item_fields(by_jid[jid], item)
            else:
                by_jid[jid] = dict(item)

    # Los contactos solo enriquecen chats ya descubiertos (no crean nuevos slots).
    for source in enrichment_sources:
        for item in source:
            if not isinstance(item, dict):
                continue
            jid = str(item.get("remoteJid") or "")
            if not jid or is_group_or_broadcast_jid(jid):
                continue
            if jid in by_jid:
                _merge_item_fields(by_jid[jid], item)

    for jid, merged in by_jid.items():
        state = chat_states.get(jid)
        if isinstance(state, dict):
            if state.get("archived") is not None:
                merged["archived"] = bool(state.get("archived"))
            if state.get("name"):
                merged["_chat_state_name"] = state.get("name")

    # Prioriza por última actividad antes de truncar para no perder chats activos.
    ordered = sorted(
        by_jid.items(),
        key=lambda kv: int(kv[1].get("lastMessageTimestamp") or 0),
        reverse=True,
    )
    return ordered[:_MAX_CHATS_PER_SYNC]


def _consolidate_lid_duplicates(
    items: list[tuple[str, dict]],
    lid_to_phone: dict[str, str],
    chat_names: dict[str, str],
) -> list[tuple[str, dict]]:
    by_jid: dict[str, dict] = {jid: dict(item) for jid, item in items}

    for lid_jid, phone in lid_to_phone.items():
        if lid_jid not in by_jid or not is_valid_whatsapp_phone(phone):
            continue
        phone_jid = f"{phone_to_evolution_number(phone)}@s.whatsapp.net"
        lid_item = by_jid.pop(lid_jid)
        lid_item["_lid_jid"] = lid_jid
        if phone_jid in by_jid:
            _merge_item_fields(by_jid[phone_jid], lid_item)
            by_jid[phone_jid]["_lid_jid"] = lid_jid
        else:
            merged = dict(lid_item)
            merged["remoteJid"] = phone_jid
            by_jid[phone_jid] = merged

    for jid, item in by_jid.items():
        saved = chat_names.get(jid) or chat_names.get(item.get("_lid_jid") or "")
        if saved and not item.get("name"):
            item["name"] = saved

    return list(by_jid.items())[:_MAX_CHATS_PER_SYNC]


def _recompute_last_message_at(db: Session, *, tenant_id) -> None:
    """Recalcula last_message_at = fecha real del último mensaje (orden tipo WhatsApp Web)."""
    from sqlalchemy import text

    try:
        db.execute(
            text(
                """
                UPDATE conversations c
                SET last_message_at = sub.mx
                FROM (
                    SELECT conversation_id, MAX(created_at) AS mx
                    FROM messages
                    WHERE tenant_id = :tid
                    GROUP BY conversation_id
                ) sub
                WHERE c.id = sub.conversation_id
                  AND c.tenant_id = :tid
                  AND (c.last_message_at IS DISTINCT FROM sub.mx)
                """
            ),
            {"tid": tenant_id},
        )
        db.flush()
    except Exception as exc:
        log.warning("No se pudo recalcular last_message_at: %s", exc)


def _apply_last_message_timestamp(
    conversation: Conversation,
    item: dict,
    *,
    instance_name: str,
    remote_jid: str,
    dsn: str,
) -> None:
    # Prioriza la fecha REAL del último mensaje en Evolution; el lastMessageTimestamp
    # del item viene del Chat.updatedAt (hora del sync) y rompe el orden.
    ts_raw = None
    if dsn:
        ts_raw = fetch_chat_last_timestamp(dsn, instance_name, remote_jid)
        lid_jid = item.get("_lid_jid")
        if ts_raw is None and lid_jid:
            ts_raw = fetch_chat_last_timestamp(dsn, instance_name, lid_jid)
    if ts_raw is None:
        return
    try:
        ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return
    if not conversation.last_message_at or ts > conversation.last_message_at:
        conversation.last_message_at = ts


def _load_message_records(
    instance_name: str,
    remote_jid: str,
    *,
    limit: int,
    since_ts: Optional[int] = None,
) -> list[dict]:
    dsn = settings.evolution_database_url
    records = fetch_stored_messages(
        dsn, instance_name, remote_jid, limit=limit, since_ts=since_ts
    )
    if records:
        return records

    if since_ts is not None:
        return []

    try:
        payload = evolution_client.find_messages(instance_name, remote_jid, limit=limit)
    except EvolutionAPIError as exc:
        log.warning("find_messages falló jid=%s: %s", remote_jid, exc)
        return []
    api_records = (payload.get("messages") or {}).get("records") or []
    if since_ts is not None:
        filtered = []
        for record in api_records:
            ts = record.get("messageTimestamp")
            try:
                if ts is not None and int(ts) >= since_ts:
                    filtered.append(record)
            except (TypeError, ValueError):
                pass
        return filtered
    return api_records


def _import_messages(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    records: list[dict],
    expected_phone: str,
    expected_jid: str,
    seen_evolution_ids: Optional[set] = None,
) -> int:
    """Importa mensajes uno a uno usando savepoints para tolerar duplicados."""
    imported = 0
    if seen_evolution_ids is None:
        seen_evolution_ids = set()

    # Eliminar duplicados en la lista de records (Evolution a veces repite mensajes)
    _seen_in_batch: set = set()
    deduped: list[dict] = []
    for rec in records:
        _k = rec.get("key") or {}
        _mid = _k.get("id") if isinstance(_k, dict) else None
        if _mid:
            if _mid in _seen_in_batch:
                continue
            _seen_in_batch.add(_mid)
        deduped.append(rec)
    records = deduped

    latest_ts: Optional[datetime] = None

    for record in reversed(records):
        parsed = _parse_evolution_message(
            record,
            expected_phone=expected_phone,
            expected_jid=expected_jid,
        )
        if not parsed:
            continue

        ts = parsed["created_at"] or datetime.now(timezone.utc)
        msg_id = parsed["message_id"]
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts

        # Saltar si ya procesamos este ID en este sync (evita duplicados cross-conv)
        if msg_id in seen_evolution_ids:
            continue

        existing = _find_existing_message(
            db,
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            evolution_message_id=msg_id,
            body=parsed["body"],
            created_at=ts,
        )
        if existing:
            seen_evolution_ids.add(msg_id)
            continue

        direction = (
            MessageDirection.OUT.value
            if parsed["from_me"]
            else MessageDirection.IN.value
        )
        source = (
            MessageSource.AGENT.value
            if parsed["from_me"]
            else MessageSource.CONTACT.value
        )
        message = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            direction=direction,
            source=source,
            body=parsed["body"],
            status=MessageStatus.RECEIVED.value
            if direction == MessageDirection.IN.value
            else MessageStatus.SENT.value,
            evolution_message_id=msg_id,
        )
        if parsed["created_at"]:
            message.created_at = parsed["created_at"]
        try:
            sp = db.begin_nested()
            db.add(message)
            db.flush()
            sp.commit()
        except Exception as exc:
            sp.rollback()
            log.debug("Mensaje %s ya existe (savepoint): %s", msg_id, exc)
            continue
        seen_evolution_ids.add(msg_id)
        imported += 1

    # last_message_at = fecha REAL del último mensaje (no la hora del sync).
    if latest_ts is not None:
        conversation.last_message_at = latest_ts

    return imported


def _dedupe_conversations_by_phone(
    db: Session,
    *,
    tenant_id,
    connection_id,
    lid_to_phone: Optional[dict[str, str]] = None,
) -> int:
    """Une conversaciones duplicadas del mismo teléfono (@lid + número real)."""
    from app.shared.core.phone import is_lid_placeholder, is_placeholder_contact_name, is_valid_whatsapp_phone

    rows = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.whatsapp_connection_id == connection_id,
        )
        .order_by(Conversation.created_at.asc())
        .all()
    )
    lid_to_phone = lid_to_phone or {}
    by_phone: dict[str, Conversation] = {}
    merged = 0

    def _merge_into(primary: Conversation, secondary: Conversation) -> None:
        nonlocal merged
        msgs = (
            db.query(Message)
            .filter(Message.conversation_id == secondary.id)
            .all()
        )
        for msg in msgs:
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

        if secondary.contact_jid and not primary.contact_jid:
            primary.contact_jid = secondary.contact_jid
        if is_placeholder_contact_name(primary.contact_name, primary.contact_phone):
            primary.contact_name = secondary.contact_name
        if secondary.last_message_at and (
            not primary.last_message_at or secondary.last_message_at > primary.last_message_at
        ):
            primary.last_message_at = secondary.last_message_at
        if secondary.is_archived:
            primary.is_archived = True
        primary.unread_count = (primary.unread_count or 0) + (secondary.unread_count or 0)

        db.delete(secondary)
        merged += 1

    for conv in rows:
        if is_valid_whatsapp_phone(conv.contact_phone):
            primary = by_phone.get(conv.contact_phone)
            if primary is None:
                by_phone[conv.contact_phone] = conv
            elif primary.id != conv.id:
                _merge_into(primary, conv)

    for conv in list(rows):
        if not is_lid_placeholder(conv.contact_phone):
            continue
        lid_jid = conv.contact_jid or ""
        phone = lid_to_phone.get(lid_jid) if lid_jid else None
        if not phone or not is_valid_whatsapp_phone(phone):
            continue
        primary = by_phone.get(phone)
        if primary and primary.id != conv.id:
            _merge_into(primary, conv)

    return merged


def sync_whatsapp_chats(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    messages_per_chat: int = 50,
    wait_for_history: bool = True,
) -> dict[str, int]:
    refresh_session_status(db, tenant, session)
    if (
        tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value
        and session.active_connection_id is None
    ):
        from app.application.conversations.whatsapp_conversation_service import ensure_whatsapp_binding_ready

        ensure_whatsapp_binding_ready(db, tenant=tenant, session=session)
    db.commit()

    if tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value:
        raise EvolutionAPIError(
            "WhatsApp no está conectado — escanea el QR antes de sincronizar"
        )

    since_ts = connection_since_unix(session.connection_started_at)

    chats, contacts, stored_chats, stored_contacts, message_index = _collect_evolution_items(
        session.instance_name,
        tenant_id=tenant.id,
        wait_for_history=wait_for_history,
        since_ts=since_ts,
    )
    api_discovered = _discover_jids_from_api(session.instance_name)
    chat_states = _fetch_chat_states(session.instance_name)
    dsn = settings.evolution_database_url
    lid_to_phone = fetch_lid_phone_mappings(dsn, session.instance_name)
    chat_names = {
        str(item.get("remoteJid")): str(item.get("name"))
        for item in stored_chats
        if isinstance(item, dict) and item.get("remoteJid") and item.get("name")
    }
    contacts_index = build_contacts_index(
        list(contacts) + list(stored_contacts) + list(stored_chats) + list(chats)
    )
    items = _merge_items(
        chats,
        contacts,
        stored_chats,
        stored_contacts,
        message_index,
        api_discovered,
        chat_states,
    )
    items = _consolidate_lid_duplicates(items, lid_to_phone, chat_names)

    conversations_imported = 0
    messages_imported = 0
    connection_id = session.active_connection_id
    # IDs de mensajes ya insertados en este sync (evita UniqueViolation cross-conv)
    seen_evolution_ids: set = set()
    owner_names = _owner_display_names(session, dsn=dsn, instance_name=session.instance_name)
    jid_names, phone_names = fetch_contact_names_index(dsn, session.instance_name)
    owner_jid = session.bound_owner_jid or ""
    owner_phone = session.phone_number or ""

    for remote_jid, item in items:
        if is_owner_jid(
            remote_jid,
            owner_jid=owner_jid,
            owner_phone=owner_phone,
        ):
            continue
        lid_jid = str(item.get("_lid_jid") or "")
        item = dict(item)
        item["_owner_names"] = owner_names
        item["_contact_names"] = jid_names
        item["_phone_names"] = phone_names
        identity = _contact_identity(
            remote_jid,
            item,
            contacts_index=contacts_index,
            instance_name=session.instance_name,
            dsn=settings.evolution_database_url,
        )
        if not identity or connection_id is None:
            continue

        phone, contact_jid, name, is_archived = identity
        if lid_jid and not contact_jid:
            contact_jid = lid_jid
        conversation = get_or_create_conversation(
            db,
            tenant_id=tenant.id,
            contact_phone=phone,
            contact_name=name,
            contact_jid=contact_jid,
            whatsapp_connection_id=connection_id,
        )
        apply_identity_to_conversation(
            conversation,
            contact_phone=phone,
            contact_name=name,
            contact_jid=contact_jid,
            is_archived=is_archived,
        )
        conversations_imported += 1

        load_jid = lid_jid or remote_jid
        # Always load without since_ts so historical messages are always imported.
        # Deduplication in _import_messages handles re-syncing efficiently.
        records = _load_message_records(
            session.instance_name,
            load_jid,
            limit=messages_per_chat,
            since_ts=None,
        )
        if not records and load_jid != remote_jid:
            records = _load_message_records(
                session.instance_name,
                remote_jid,
                limit=messages_per_chat,
                since_ts=None,
            )
        messages_imported += _import_messages(
            db,
            tenant=tenant,
            conversation=conversation,
            records=records,
            expected_phone=phone,
            expected_jid=contact_jid,
            seen_evolution_ids=seen_evolution_ids,
        )
        # Fallback de orden solo para chats sin ningún mensaje (van al fondo).
        if conversation.last_message_at is None:
            _apply_last_message_timestamp(
                conversation,
                item,
                instance_name=session.instance_name,
                remote_jid=remote_jid,
                dsn=dsn,
            )

        publish_conversation_updated(tenant.id, conversation)
        try:
            db.commit()
        except Exception as exc:
            log.warning("Commit conv %s falló: %s — reintentando", phone, exc)
            db.rollback()

    enrich_stats = enrich_tenant_conversations(
        db,
        tenant=tenant,
        session=session,
        contacts_index=None,  # datos frescos de Evolution al finalizar el sync
    )
    merged = _dedupe_conversations_by_phone(
        db,
        tenant_id=tenant.id,
        connection_id=connection_id,
        lid_to_phone=lid_to_phone,
    )

    db.flush()
    _recompute_last_message_at(db, tenant_id=tenant.id)
    return {
        "conversations_imported": conversations_imported,
        "messages_imported": messages_imported,
        "contacts_enriched": enrich_stats["enriched"],
        "names_fixed": enrich_stats["names_fixed"],
        "phones_fixed": enrich_stats["phones_fixed"],
        "conversations_merged": merged,
        "evolution_chats": len(chats),
        "evolution_contacts": len(contacts),
        "evolution_stored_chats": len(stored_chats),
        "evolution_stored_contacts": len(stored_contacts),
        "evolution_message_chats": len(message_index),
        "evolution_api_discovered": len(api_discovered),
    }

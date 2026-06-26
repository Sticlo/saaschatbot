from __future__ import annotations

import logging
from typing import Any, Optional

from app.shared.core.phone import (
    is_owner_display_name,
    is_placeholder_contact_name,
    jid_to_phone,
)
from app.infrastructure.evolution.evolution_store import register_contact_name

log = logging.getLogger(__name__)

_CACHE_PREFIX = "wa:contact_names"
_CACHE_TTL = 86400 * 30


def _cache_key(instance_name: str) -> str:
    # instance_name is derived from tenant_id (see instance_name_for_tenant),
    # so this key is naturally scoped per tenant. No cross-tenant leakage possible.
    return f"{_CACHE_PREFIX}:{instance_name}"


_MAX_NAME_LEN = 200


def _clean_name(name: str, *, phone: str = "", owner_names: Optional[set[str]] = None) -> str:
    cleaned = str(name or "").strip()[:_MAX_NAME_LEN]
    if not cleaned:
        return ""
    if owner_names and is_owner_display_name(cleaned, owner_names):
        return ""
    if is_placeholder_contact_name(cleaned, phone):
        return ""
    return cleaned


def extract_name_from_record(record: dict) -> str:
    """WhatsApp profile name from an Evolution/Baileys payload.

    Priority: pushName (WA profile) > verifiedName > notify > name (phone book).
    Phone book "name" is last because it is user-typed and can be misassociated with
    the wrong @lid contact — especially in contacts.set/upsert events.
    """
    if not isinstance(record, dict):
        return ""
    last_msg = record.get("lastMessage") if isinstance(record.get("lastMessage"), dict) else {}
    return str(
        record.get("pushName")
        or record.get("verifiedName")
        or record.get("notify")
        or last_msg.get("pushName")
        or record.get("name")
        or ""
    ).strip()


def extract_name_from_message_record(record: dict) -> tuple[str, str]:
    """Devuelve (remote_jid, pushName) desde un mensaje Evolution."""
    if not isinstance(record, dict):
        return "", ""
    key = record.get("key") or {}
    if not isinstance(key, dict):
        return "", ""
    jid = str(key.get("remoteJid") or key.get("remoteJidAlt") or "")
    if not jid or bool(key.get("fromMe")):
        return "", ""
    name = str(record.get("pushName") or "").strip()
    return jid, name


def remember_contact_name(
    instance_name: str,
    jid: str,
    name: str,
    *,
    phone: str = "",
    owner_names: Optional[set[str]] = None,
) -> bool:
    cleaned = _clean_name(name, phone=phone, owner_names=owner_names)
    if not cleaned or not jid:
        return False
    try:
        from app.infrastructure.cache.redis_client import get_redis

        key = _cache_key(instance_name)
        get_redis().hset(key, jid, cleaned)
        get_redis().expire(key, _CACHE_TTL)
        phone_val = jid_to_phone(jid) if jid.endswith("@s.whatsapp.net") else phone
        if phone_val:
            get_redis().hset(key, f"phone:{phone_val}", cleaned)
        return True
    except Exception as exc:
        log.debug("remember_contact_name %s: %s", jid, exc)
        return False


def remember_from_record(
    instance_name: str,
    record: dict,
    *,
    owner_names: Optional[set[str]] = None,
) -> bool:
    jid = str(record.get("remoteJid") or record.get("id") or "")
    name = extract_name_from_record(record)
    phone = jid_to_phone(jid) if jid.endswith("@s.whatsapp.net") else ""
    return remember_contact_name(
        instance_name, jid, name, phone=phone, owner_names=owner_names
    )


def remember_from_message_record(
    instance_name: str,
    record: dict,
    *,
    owner_names: Optional[set[str]] = None,
) -> bool:
    jid, name = extract_name_from_message_record(record)
    if not jid or not name:
        return False
    return remember_contact_name(instance_name, jid, name, owner_names=owner_names)


def load_cached_names(instance_name: str) -> tuple[dict[str, str], dict[str, str]]:
    """jid→nombre y teléfono E.164→nombre desde Redis."""
    jid_names: dict[str, str] = {}
    phone_names: dict[str, str] = {}
    try:
        from app.infrastructure.cache.redis_client import get_redis

        raw = get_redis().hgetall(_cache_key(instance_name)) or {}
        for key, val in raw.items():
            k = key.decode() if isinstance(key, bytes) else str(key)
            v = val.decode() if isinstance(val, bytes) else str(val)
            v = v.strip()
            if not v:
                continue
            if k.startswith("phone:"):
                phone_names[k[6:]] = v
            else:
                jid_names[k] = v
                register_contact_name(jid_names, phone_names, k, v)
    except Exception as exc:
        log.debug("load_cached_names: %s", exc)
    return jid_names, phone_names


def apply_cached_names_to_conversations(
    conversations: list,
    instance_name: str,
    *,
    owner_names: Optional[set[str]] = None,
) -> int:
    jid_names, phone_names = load_cached_names(instance_name)
    if not jid_names and not phone_names:
        return 0
    from app.application.sync.contact_identity_service import ContactNamesLookup

    lookup = ContactNamesLookup(
        jid_names=jid_names,
        phone_names=phone_names,
        lid_to_phone={},
        phone_to_lid={},
    )
    from app.application.sync.contact_identity_service import apply_names_lookup_to_conversations

    return apply_names_lookup_to_conversations(
        conversations, lookup, owner_names=owner_names
    )


def ingest_message_batch(
    instance_name: str,
    data: Any,
    *,
    owner_names: Optional[set[str]] = None,
) -> int:
    # messages.set payload: {"messages": [...], "isLatest": ..., "progress": ...}
    if isinstance(data, dict):
        records = data.get("messages") or []
        if not isinstance(records, list):
            records = []
    elif isinstance(data, list):
        records = data
    else:
        records = []
    stored = 0
    for record in records:
        if isinstance(record, dict) and remember_from_message_record(
            instance_name, record, owner_names=owner_names
        ):
            stored += 1
    if stored:
        log.info("ingest_message_batch inst=%s stored=%d/%d names", instance_name, stored, len(records))
    return stored


def backfill_names_from_evolution_api(
    instance_name: str,
    *,
    max_pages: int = 40,
    owner_names: Optional[set[str]] = None,
) -> int:
    """Escanea mensajes en Evolution API y guarda pushName por JID."""
    from app.infrastructure.evolution.evolution_client import evolution_client

    stored = 0
    for page in range(1, max_pages + 1):
        try:
            payload = evolution_client.find_recent_messages(
                instance_name, limit=500, page=page
            )
        except Exception:
            break
        records = (payload.get("messages") or {}).get("records") or []
        if not records:
            break
        stored += ingest_message_batch(
            instance_name, records, owner_names=owner_names
        )
        pages = (payload.get("messages") or {}).get("pages") or 1
        if page >= pages:
            break
    return stored


def backfill_names_from_evolution_db(
    instance_name: str,
    *,
    owner_names: Optional[set[str]] = None,
) -> int:
    """Lee pushName e info de Chat de la BD Evolution directamente y cachea nombres."""
    from app.config import settings
    from app.infrastructure.evolution.evolution_store import _normalize_dsn

    dsn = getattr(settings, "evolution_database_url", None)
    if not dsn:
        return 0
    stored = 0
    try:
        import psycopg  # type: ignore

        with psycopg.connect(_normalize_dsn(dsn), connect_timeout=3) as conn:
            with conn.cursor() as cur:
                # 1) pushName de mensajes entrantes (no fromMe)
                cur.execute(
                    """
                    SELECT DISTINCT ON (m.key->>'remoteJid')
                        m.key->>'remoteJid' AS jid,
                        m."pushName"
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND COALESCE((m.key->>'fromMe')::boolean, false) = false
                      AND m."pushName" IS NOT NULL AND m."pushName" <> ''
                    ORDER BY m.key->>'remoteJid', m."messageTimestamp" DESC
                    LIMIT 5000
                    """,
                    (instance_name,),
                )
                for jid, push_name in cur.fetchall():
                    jid = str(jid or "").strip()
                    name = str(push_name or "").strip()
                    if jid and name:
                        phone = jid_to_phone(jid) if jid.endswith("@s.whatsapp.net") else ""
                        cleaned = _clean_name(name, phone=phone, owner_names=owner_names)
                        if cleaned:
                            remember_contact_name(
                                instance_name, jid, cleaned,
                                phone=phone, owner_names=owner_names,
                            )
                            stored += 1

                # 2) Chat.name (agenda del celular si Evolution lo guarda)
                cur.execute(
                    """
                    SELECT c."remoteJid", c.name
                    FROM "Chat" c
                    JOIN "Instance" i ON i.id = c."instanceId"
                    WHERE i.name = %s
                      AND c.name IS NOT NULL AND c.name <> ''
                      AND c."remoteJid" NOT LIKE '%%@g.us'
                    LIMIT 5000
                    """,
                    (instance_name,),
                )
                for jid, name in cur.fetchall():
                    jid = str(jid or "").strip()
                    name = str(name or "").strip()
                    if jid and name:
                        phone = jid_to_phone(jid) if jid.endswith("@s.whatsapp.net") else ""
                        cleaned = _clean_name(name, phone=phone, owner_names=owner_names)
                        if cleaned:
                            remember_contact_name(
                                instance_name, jid, cleaned,
                                phone=phone, owner_names=owner_names,
                            )
                            stored += 1

                # 3) Contact.pushName
                cur.execute(
                    """
                    SELECT c."remoteJid", c."pushName"
                    FROM "Contact" c
                    JOIN "Instance" i ON i.id = c."instanceId"
                    WHERE i.name = %s
                      AND c."pushName" IS NOT NULL AND c."pushName" <> ''
                      AND c."remoteJid" NOT LIKE '%%@g.us'
                    LIMIT 5000
                    """,
                    (instance_name,),
                )
                for jid, push_name in cur.fetchall():
                    jid = str(jid or "").strip()
                    name = str(push_name or "").strip()
                    if jid and name:
                        phone = jid_to_phone(jid) if jid.endswith("@s.whatsapp.net") else ""
                        cleaned = _clean_name(name, phone=phone, owner_names=owner_names)
                        if cleaned:
                            remember_contact_name(
                                instance_name, jid, cleaned,
                                phone=phone, owner_names=owner_names,
                            )
                            stored += 1
    except Exception as exc:
        log.warning("backfill_names_from_evolution_db %s: %s", instance_name, exc)
    if stored:
        log.info("backfill_names_from_evolution_db inst=%s stored=%d", instance_name, stored)
    return stored

from __future__ import annotations

import logging
import re
from typing import Optional

import psycopg

log = logging.getLogger(__name__)

_CONNECT_KWARGS = {"connect_timeout": 3}
_DEFAULT_LIMIT = 1000


def _normalize_dsn(url: str) -> str:
    return re.sub(r"^postgresql\+psycopg://", "postgresql://", url.strip())


def purge_instance_stored_data(dsn: str, instance_name: str) -> None:
    """Borra chats/mensajes/contactos Evolution de una instancia (otro celular vinculado)."""
    if not dsn:
        return
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM "Message" m
                    USING "Instance" i
                    WHERE m."instanceId" = i.id AND i.name = %s
                    """,
                    (instance_name,),
                )
                cur.execute(
                    """
                    DELETE FROM "Chat" c
                    USING "Instance" i
                    WHERE c."instanceId" = i.id AND i.name = %s
                    """,
                    (instance_name,),
                )
                cur.execute(
                    """
                    DELETE FROM "Contact" c
                    USING "Instance" i
                    WHERE c."instanceId" = i.id AND i.name = %s
                    """,
                    (instance_name,),
                )
            conn.commit()
        log.info("Evolution DB limpiada para instancia=%s", instance_name)
    except Exception as exc:
        log.warning("No se pudo limpiar Evolution DB instancia=%s: %s", instance_name, exc)


def connection_since_unix(connection_started_at) -> Optional[int]:
    if connection_started_at is None:
        return None
    try:
        return int(connection_started_at.timestamp())
    except (TypeError, ValueError, OSError):
        return None


def fetch_stored_chats(dsn: str, instance_name: str, *, limit: int = _DEFAULT_LIMIT) -> list[dict]:
    """Lee chats guardados en la DB de Evolution (tabla Chat, sin depender de Message)."""
    if not dsn:
        return []
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c."remoteJid", c.name, c."updatedAt"
                    FROM "Chat" c
                    JOIN "Instance" i ON i.id = c."instanceId"
                    WHERE i.name = %s
                    ORDER BY c."updatedAt" DESC NULLS LAST
                    LIMIT %s
                    """,
                    (instance_name, limit),
                )
                rows = cur.fetchall()
        items: list[dict] = []
        for row in rows:
            if not row[0]:
                continue
            item: dict = {"remoteJid": row[0], "name": row[1]}
            if row[2] is not None:
                try:
                    item["lastMessageTimestamp"] = int(row[2].timestamp())
                except (TypeError, ValueError, OSError):
                    pass
            items.append(item)
        return items
    except Exception as exc:
        log.warning("No se pudo leer chats de Evolution DB: %s", exc)
        return []


def fetch_stored_contacts(dsn: str, instance_name: str, *, limit: int = _DEFAULT_LIMIT) -> list[dict]:
    if not dsn:
        return []
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                # Intentamos leer la columna "name" (agenda/phonebook) si existe en esta versión
                # de Evolution. Si no existe, psycopg lanza UndefinedColumn y caemos al fallback.
                try:
                    cur.execute(
                        """
                        SELECT c."remoteJid",
                               COALESCE(c.name, c."pushName") AS best_name,
                               c."pushName"
                        FROM "Contact" c
                        JOIN "Instance" i ON i.id = c."instanceId"
                        WHERE i.name = %s
                          AND (c."pushName" IS NOT NULL OR c.name IS NOT NULL)
                          AND COALESCE(c.name, c."pushName", '') <> ''
                        ORDER BY c."updatedAt" DESC NULLS LAST
                        LIMIT %s
                        """,
                        (instance_name, limit),
                    )
                    rows = cur.fetchall()
                    has_name_col = True
                except Exception:
                    conn.rollback()
                    has_name_col = False
                    rows = []

                if not has_name_col:
                    cur.execute(
                        """
                        SELECT c."remoteJid", c."pushName", c."pushName"
                        FROM "Contact" c
                        JOIN "Instance" i ON i.id = c."instanceId"
                        WHERE i.name = %s
                          AND c."pushName" IS NOT NULL
                          AND c."pushName" <> ''
                        ORDER BY c."updatedAt" DESC NULLS LAST
                        LIMIT %s
                        """,
                        (instance_name, limit),
                    )
                    rows = cur.fetchall()

        items: list[dict] = []
        for row in rows:
            if not row[0]:
                continue
            best_name = str(row[1] or "").strip()
            push_name = str(row[2] or "").strip()
            item: dict = {"remoteJid": row[0]}
            if push_name:
                item["pushName"] = push_name
            if best_name:
                item["name"] = best_name
            if best_name or push_name:
                items.append(item)
        return items
    except Exception as exc:
        log.warning("No se pudo leer contactos de Evolution DB: %s", exc)
        return []


def register_contact_name(
    jid_names: dict[str, str],
    phone_names: dict[str, str],
    jid: str,
    name: str,
) -> None:
    from app.shared.core.phone import jid_to_phone

    cleaned = str(name or "").strip()
    if not jid or not cleaned:
        return
    jid_names[str(jid)] = cleaned
    if str(jid).endswith("@s.whatsapp.net"):
        phone = jid_to_phone(str(jid))
        if phone:
            phone_names[phone] = cleaned


def fetch_contact_names_index(
    dsn: str, instance_name: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Mapas jid→nombre y teléfono E.164→nombre (Contact, Chat, mensajes, @lid)."""
    return fetch_comprehensive_names_index(dsn, instance_name)


def fetch_comprehensive_names_index(
    dsn: str,
    instance_name: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Índice unificado de nombres desde Evolution DB (agenda, chats, pushName)."""
    jid_names: dict[str, str] = {}
    phone_names: dict[str, str] = {}
    if not dsn:
        return jid_names, phone_names
    try:
        for item in fetch_stored_contacts(dsn, instance_name, limit=20000):
            jid = str(item.get("remoteJid") or "")
            best = str(item.get("name") or item.get("pushName") or "").strip()
            register_contact_name(jid_names, phone_names, jid, best)

        for jid, name in fetch_stored_chat_names_index(dsn, instance_name, limit=20000).items():
            register_contact_name(jid_names, phone_names, jid, name)

        for item in fetch_message_chat_index(dsn, instance_name, limit=10000):
            jid = str(item.get("remoteJid") or "")
            push = str(item.get("pushName") or "").strip()
            if jid and push and jid not in jid_names:
                register_contact_name(jid_names, phone_names, jid, push)

        lid_to_phone, _ = fetch_bidirectional_lid_mappings(dsn, instance_name)
        for lid_jid, phone in lid_to_phone.items():
            lid_name = jid_names.get(lid_jid)
            if lid_name and phone and phone not in phone_names:
                phone_names[phone] = lid_name
                from app.shared.core.phone import phone_to_evolution_number

                phone_jid = f"{phone_to_evolution_number(phone)}@s.whatsapp.net"
                jid_names.setdefault(phone_jid, lid_name)
    except Exception as exc:
        log.warning("No se pudo leer índice de nombres: %s", exc)
    return jid_names, phone_names


def fetch_stored_counts(dsn: str, instance_name: str) -> dict[str, int]:
    if not dsn:
        return {"chats": 0, "contacts": 0, "messages": 0}
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM "Chat" c
                         JOIN "Instance" i ON i.id = c."instanceId"
                         WHERE i.name = %s),
                        (SELECT COUNT(*) FROM "Contact" c
                         JOIN "Instance" i ON i.id = c."instanceId"
                         WHERE i.name = %s),
                        (SELECT COUNT(*) FROM "Message" m
                         JOIN "Instance" i ON i.id = m."instanceId"
                         WHERE i.name = %s)
                    """,
                    (instance_name, instance_name, instance_name),
                )
                row = cur.fetchone()
        return {"chats": row[0] or 0, "contacts": row[1] or 0, "messages": row[2] or 0}
    except Exception as exc:
        log.warning("No se pudo leer conteos Evolution: %s", exc)
        return {"chats": 0, "contacts": 0, "messages": 0}


def fetch_message_chat_index(
    dsn: str,
    instance_name: str,
    *,
    limit: int = _DEFAULT_LIMIT,
    since_ts: Optional[int] = None,
) -> list[dict]:
    """Lista conversaciones a partir de mensajes guardados (como findChats pero sin INNER JOIN API)."""
    if not dsn:
        return []
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        m.key->>'remoteJid' AS remote_jid,
                        MAX(m."messageTimestamp") AS last_ts,
                        (array_agg(m."pushName" ORDER BY m."messageTimestamp" DESC)
                            FILTER (
                                WHERE COALESCE((m.key->>'fromMe')::boolean, false) = false
                                  AND m."pushName" IS NOT NULL
                                  AND m."pushName" <> ''
                            ))[1] AS push_name
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND m.key->>'remoteJid' IS NOT NULL
                      AND m.key->>'remoteJid' NOT LIKE '%%@g.us'
                      AND m.key->>'remoteJid' NOT LIKE '%%broadcast%%'
                      {since_clause}
                    GROUP BY m.key->>'remoteJid'
                    ORDER BY last_ts DESC NULLS LAST
                    LIMIT %s
                    """.format(
                        since_clause=(
                            'AND m."messageTimestamp" >= %s' if since_ts else ""
                        )
                    ),
                    (instance_name, since_ts, limit)
                    if since_ts
                    else (instance_name, limit),
                )
                rows = cur.fetchall()
        return [
            {
                "remoteJid": row[0],
                "pushName": row[2],
                "lastMessageTimestamp": row[1],
            }
            for row in rows
            if row[0]
        ]
    except Exception as exc:
        log.warning("No se pudo leer índice de chats desde mensajes Evolution: %s", exc)
        return []


def fetch_lid_jid_for_phone(dsn: str, instance_name: str, phone_e164: str) -> Optional[str]:
    """Busca el @lid asociado a un teléfono concreto en mensajes Evolution."""
    if not dsn or not phone_e164:
        return None
    from app.shared.core.phone import jid_to_phone, phone_to_evolution_number

    digits = phone_to_evolution_number(phone_e164)
    phone_jid = f"{digits}@s.whatsapp.net"
    patterns = [phone_jid, digits, phone_e164]
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m.key->>'remoteJid'
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND m.key->>'remoteJid' LIKE '%%@lid'
                      AND (
                        m.key->>'remoteJidAlt' = %s
                        OR m.key->>'senderPn' = %s
                        OR m.key->>'senderPn' = %s
                        OR m.key->>'participantPn' = %s
                        OR m.key->>'participantPn' = %s
                        OR m.key->>'participantAlt' = %s
                        OR m.key->>'participantAlt' = %s
                      )
                    ORDER BY m."messageTimestamp" DESC
                    LIMIT 1
                    """,
                    (
                        instance_name,
                        phone_jid,
                        phone_jid,
                        digits,
                        phone_jid,
                        digits,
                        phone_jid,
                        digits,
                    ),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0])

                cur.execute(
                    """
                    SELECT m.key->>'remoteJidAlt'
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND m.key->>'remoteJid' = %s
                      AND m.key->>'remoteJidAlt' LIKE '%%@lid'
                    ORDER BY m."messageTimestamp" DESC
                    LIMIT 1
                    """,
                    (instance_name, phone_jid),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0])
    except Exception as exc:
        log.warning("No se pudo resolver @lid para teléfono=%s: %s", phone_e164, exc)
    return None


def infer_phone_for_lid_from_timeline(
    dsn: str,
    instance_name: str,
    lid_jid: str,
    *,
    outbound_gap_seconds: int = 180,
) -> Optional[str]:
    """Infiere teléfono cuando WhatsApp cambia de @s.whatsapp.net a @lid en el mismo hilo.

    Requiere un único chat @s.whatsapp.net con mensaje saliente (fromMe) justo antes
    del primer mensaje @lid — evita fusiones Ana/Diana por nombre.
    """
    if not dsn or not lid_jid.endswith("@lid"):
        return None
    try:
        from app.shared.core.phone import is_valid_whatsapp_phone, jid_to_phone, normalize_phone

        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT MIN(m."messageTimestamp")
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s AND m.key->>'remoteJid' = %s
                    """,
                    (instance_name, lid_jid),
                )
                row = cur.fetchone()
                if not row or row[0] is None:
                    return None
                first_lid_ts = int(row[0])

                cur.execute(
                    """
                    SELECT DISTINCT m.key->>'remoteJid'
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND m.key->>'remoteJid' LIKE '%%@s.whatsapp.net'
                      AND m.key->>'fromMe' = 'true'
                      AND m."messageTimestamp" BETWEEN %s AND %s
                    """,
                    (
                        instance_name,
                        first_lid_ts - outbound_gap_seconds,
                        first_lid_ts,
                    ),
                )
                phone_jids = [str(r[0]) for r in cur.fetchall() if r and r[0]]

        phones: list[str] = []
        for jid in phone_jids:
            phone = jid_to_phone(jid)
            if phone and is_valid_whatsapp_phone(phone):
                norm = normalize_phone(phone)
                if norm not in phones:
                    phones.append(norm)
        if len(phones) == 1:
            return phones[0]
        return None
    except Exception as exc:
        log.warning("No se pudo inferir teléfono por timeline lid=%s: %s", lid_jid, exc)
        return None


def fetch_lid_alt_phone(dsn: str, instance_name: str, lid_jid: str) -> Optional[str]:
    """Si un chat usa @lid, busca remoteJidAlt con teléfono real en mensajes Evolution."""
    if not dsn or not lid_jid or not lid_jid.endswith("@lid"):
        return None
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m.key->>'remoteJidAlt'
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND m.key->>'remoteJid' = %s
                      AND m.key->>'remoteJidAlt' LIKE '%%@s.whatsapp.net'
                    ORDER BY m."messageTimestamp" DESC
                    LIMIT 1
                    """,
                    (instance_name, lid_jid),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    cur.execute(
                        """
                        SELECT m.key->>'remoteJid'
                        FROM "Message" m
                        JOIN "Instance" i ON i.id = m."instanceId"
                        WHERE i.name = %s
                          AND m.key->>'remoteJidAlt' = %s
                          AND m.key->>'remoteJid' LIKE '%%@s.whatsapp.net'
                        ORDER BY m."messageTimestamp" DESC
                        LIMIT 1
                        """,
                        (instance_name, lid_jid),
                    )
                    row = cur.fetchone()
        if not row or not row[0]:
            return None
        from app.shared.core.phone import jid_to_phone

        return jid_to_phone(str(row[0]))
    except Exception as exc:
        log.warning("No se pudo resolver teléfono para lid=%s: %s", lid_jid, exc)
        return None


def fetch_lid_phone_mappings(dsn: str, instance_name: str) -> dict[str, str]:
    """Mapa lid_jid → teléfono E.164 a partir de mensajes Evolution."""
    lid_to_phone, _ = fetch_bidirectional_lid_mappings(dsn, instance_name)
    return lid_to_phone


def fetch_bidirectional_lid_mappings(
    dsn: str, instance_name: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Mapas lid_jid↔teléfono E.164 solo cuando la relación es unívoca.

    Evolution puede conservar claves contradictorias de sesiones antiguas. Si un
    @lid apunta a varios teléfonos (o viceversa), se omite por completo para no
    mezclar personas.
    """
    lid_to_phone: dict[str, str] = {}
    phone_to_lid: dict[str, str] = {}
    lid_candidates: dict[str, set[str]] = {}
    phone_candidates: dict[str, set[str]] = {}
    if not dsn:
        return lid_to_phone, phone_to_lid
    try:
        from app.shared.core.phone import (
            is_untrusted_contact_phone,
            is_valid_whatsapp_phone,
            jid_to_phone,
            normalize_phone,
            phone_to_evolution_number,
        )

        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (m.key->>'remoteJid', m.key->>'remoteJidAlt')
                        m.key->>'remoteJid' AS main_jid,
                        m.key->>'remoteJidAlt' AS alt_jid
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND (
                        (m.key->>'remoteJid' LIKE '%%@lid'
                         AND m.key->>'remoteJidAlt' LIKE '%%@s.whatsapp.net')
                        OR
                        (m.key->>'remoteJid' LIKE '%%@s.whatsapp.net'
                         AND m.key->>'remoteJidAlt' LIKE '%%@lid')
                      )
                    ORDER BY m.key->>'remoteJid', m.key->>'remoteJidAlt',
                             m."messageTimestamp" DESC
                    """,
                    (instance_name,),
                )
                def _record(lid_jid: str, phone_jid_or_num: str) -> None:
                    phone = jid_to_phone(phone_jid_or_num)
                    if (
                        not phone
                        or not lid_jid.endswith("@lid")
                        or not is_valid_whatsapp_phone(phone)
                        or is_untrusted_contact_phone(phone)
                    ):
                        return
                    normalized = normalize_phone(phone)
                    lid_candidates.setdefault(lid_jid, set()).add(normalized)
                    phone_candidates.setdefault(normalized, set()).add(lid_jid)

                for main_jid, alt_jid in cur.fetchall():
                    if not main_jid or not alt_jid:
                        continue
                    main_jid = str(main_jid)
                    alt_jid = str(alt_jid)
                    if main_jid.endswith("@lid") and alt_jid.endswith("@s.whatsapp.net"):
                        _record(main_jid, alt_jid)
                    elif alt_jid.endswith("@lid") and main_jid.endswith("@s.whatsapp.net"):
                        _record(alt_jid, main_jid)

                # Fuente extra: Baileys reciente adjunta el teléfono del remitente/participante
                # en el propio key aunque remoteJid sea @lid (senderPn / participantPn / participantAlt).
                # Si los campos no existen, ->> retorna NULL y simplemente no aporta filas.
                cur.execute(
                    """
                    SELECT DISTINCT
                        m.key->>'remoteJid'   AS remote_jid,
                        m.key->>'senderPn'    AS sender_pn,
                        m.key->>'participantPn' AS participant_pn,
                        m.key->>'participantAlt' AS participant_alt
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND m.key->>'remoteJid' LIKE '%%@lid'
                      AND COALESCE(
                            m.key->>'senderPn',
                            m.key->>'participantPn',
                            m.key->>'participantAlt'
                          ) IS NOT NULL
                    """,
                    (instance_name,),
                )
                for remote_jid, sender_pn, participant_pn, participant_alt in cur.fetchall():
                    lid_jid = str(remote_jid or "")
                    phone_src = str(sender_pn or participant_pn or participant_alt or "")
                    if lid_jid and phone_src:
                        _record(lid_jid, phone_src)

        for lid_jid, phones in lid_candidates.items():
            if len(phones) != 1:
                log.warning(
                    "Mapeo @lid ambiguo omitido instancia=%s lid=%s phones=%s",
                    instance_name,
                    lid_jid,
                    sorted(phones),
                )
                continue
            phone = next(iter(phones))
            lids = phone_candidates.get(phone, set())
            if len(lids) != 1:
                log.warning(
                    "Mapeo teléfono ambiguo omitido instancia=%s phone=%s lids=%s",
                    instance_name,
                    phone,
                    sorted(lids),
                )
                continue
            lid_to_phone[lid_jid] = phone
            phone_to_lid[phone] = lid_jid
            phone_to_lid[phone_to_evolution_number(phone)] = lid_jid
    except Exception as exc:
        log.warning("No se pudo leer mapeos @lid bidireccionales: %s", exc)
    return lid_to_phone, phone_to_lid


def fetch_stored_chat_name(
    dsn: str, instance_name: str, remote_jid: str
) -> Optional[str]:
    """Nombre guardado en agenda (tabla Chat) para un JID."""
    if not dsn or not remote_jid:
        return None
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.name
                    FROM "Chat" c
                    JOIN "Instance" i ON i.id = c."instanceId"
                    WHERE i.name = %s AND c."remoteJid" = %s
                    LIMIT 1
                    """,
                    (instance_name, remote_jid),
                )
                row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception as exc:
        log.warning("No se pudo leer nombre Chat jid=%s: %s", remote_jid, exc)
    return None


def fetch_chat_last_timestamp(
    dsn: str, instance_name: str, remote_jid: str
) -> Optional[int]:
    """Último messageTimestamp en Evolution para un chat (sin filtro de vinculación)."""
    if not dsn or not remote_jid:
        return None
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT MAX(m."messageTimestamp")
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s AND m.key->>'remoteJid' = %s
                    """,
                    (instance_name, remote_jid),
                )
                row = cur.fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception as exc:
        log.warning("No se pudo leer last_ts jid=%s: %s", remote_jid, exc)
    return None


def fetch_stored_chat_names_index(
    dsn: str, instance_name: str, *, limit: int = 10000
) -> dict[str, str]:
    """Mapa remoteJid → nombre de agenda (tabla Chat)."""
    names: dict[str, str] = {}
    for item in fetch_stored_chats(dsn, instance_name, limit=limit):
        jid = str(item.get("remoteJid") or "")
        name = str(item.get("name") or "").strip()
        if jid and name:
            names[jid] = name
    return names


def fetch_owner_push_names(dsn: str, instance_name: str, *, limit: int = 20) -> set[str]:
    """Nombres del dueño de la sesión, tomados de mensajes salientes (fromMe=true).

    El pushName de un mensaje fromMe es siempre el nombre del dueño del teléfono,
    por lo que es la fuente más confiable para no usarlo como nombre de contacto.
    """
    names: set[str] = set()
    if not dsn:
        return names
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT m."pushName"
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND COALESCE((m.key->>'fromMe')::boolean, false) = true
                      AND m."pushName" IS NOT NULL
                      AND m."pushName" <> ''
                    ORDER BY m."pushName"
                    LIMIT %s
                    """,
                    (instance_name, limit),
                )
                for (push,) in cur.fetchall():
                    cleaned = str(push or "").strip()
                    if cleaned:
                        names.add(cleaned)
    except Exception as exc:
        log.warning("No se pudo leer pushName del dueño: %s", exc)
    return names


def fetch_contact_push_name(dsn: str, instance_name: str, remote_jid: str) -> Optional[str]:
    if not dsn or not remote_jid:
        return None
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c."pushName"
                    FROM "Contact" c
                    JOIN "Instance" i ON i.id = c."instanceId"
                    WHERE i.name = %s AND c."remoteJid" = %s
                    LIMIT 1
                    """,
                    (instance_name, remote_jid),
                )
                row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception as exc:
        log.warning("No se pudo leer pushName contact=%s: %s", remote_jid, exc)
    return None


def fetch_lid_push_name(dsn: str, instance_name: str, lid_jid: str) -> Optional[str]:
    if not dsn or not lid_jid:
        return None
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m."pushName"
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND m.key->>'remoteJid' = %s
                      AND COALESCE((m.key->>'fromMe')::boolean, false) = false
                      AND m."pushName" IS NOT NULL
                      AND m."pushName" <> ''
                    ORDER BY m."messageTimestamp" DESC
                    LIMIT 1
                    """,
                    (instance_name, lid_jid),
                )
                row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception as exc:
        log.warning("No se pudo leer pushName lid=%s: %s", lid_jid, exc)
    return None


def fetch_evolution_message_by_id(
    dsn: str,
    instance_name: str,
    message_id: str,
    remote_jid: str,
) -> Optional[dict]:
    """Retorna {key, message} de Evolution para un ID dado, buscando por remoteJid o remoteJidAlt."""
    if not dsn or not message_id or not remote_jid:
        return None
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                # Busca primero por remoteJid exacto
                cur.execute(
                    """
                    SELECT m.key, m.message
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND m.key->>'id' = %s
                      AND m.key->>'remoteJid' = %s
                    LIMIT 1
                    """,
                    (instance_name, message_id, remote_jid),
                )
                row = cur.fetchone()
                if not row:
                    cur.execute(
                        """
                        SELECT m.key, m.message
                        FROM "Message" m
                        JOIN "Instance" i ON i.id = m."instanceId"
                        WHERE i.name = %s AND m.key->>'id' = %s
                        LIMIT 1
                        """,
                        (instance_name, message_id),
                    )
                    row = cur.fetchone()
        if not row or not row[0]:
            return None
        key = row[0] if isinstance(row[0], dict) else {}
        message = row[1] if isinstance(row[1], dict) else {}
        return {"key": key, "message": message}
    except Exception as exc:
        log.warning("fetch_evolution_message_by_id id=%s: %s", message_id, exc)
        return None


def fetch_stored_messages(
    dsn: str,
    instance_name: str,
    remote_jid: str,
    *,
    limit: int = 100,
    since_ts: Optional[int] = None,
) -> list[dict]:
    if not dsn:
        return []
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                if since_ts:
                    cur.execute(
                        """
                        SELECT m.key, m."pushName", m.message, m."messageTimestamp"
                        FROM "Message" m
                        JOIN "Instance" i ON i.id = m."instanceId"
                        WHERE i.name = %s
                          AND m.key->>'remoteJid' = %s
                          AND m."messageTimestamp" >= %s
                        ORDER BY m."messageTimestamp" DESC
                        LIMIT %s
                        """,
                        (instance_name, remote_jid, since_ts, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT m.key, m."pushName", m.message, m."messageTimestamp"
                        FROM "Message" m
                        JOIN "Instance" i ON i.id = m."instanceId"
                        WHERE i.name = %s
                          AND m.key->>'remoteJid' = %s
                        ORDER BY m."messageTimestamp" DESC
                        LIMIT %s
                        """,
                        (instance_name, remote_jid, limit),
                    )
                rows = cur.fetchall()
        records = []
        for key, push_name, message, ts in rows:
            if not isinstance(key, dict):
                continue
            records.append(
                {
                    "key": key,
                    "pushName": push_name,
                    "message": message if isinstance(message, dict) else {},
                    "messageTimestamp": ts,
                }
            )
        return records
    except Exception as exc:
        log.warning("No se pudo leer mensajes Evolution jid=%s: %s", remote_jid, exc)
        return []


def fetch_recent_stored_messages(
    dsn: str,
    instance_name: str,
    *,
    since_ts: int,
    limit: int = 120,
) -> list[dict]:
    """Mensajes recientes de todos los chats (pull en vivo sin webhook)."""
    if not dsn or since_ts <= 0:
        return []
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m.key, m."pushName", m.message, m."messageTimestamp"
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND m."messageTimestamp" >= %s
                      AND m.key->>'remoteJid' IS NOT NULL
                      AND m.key->>'remoteJid' NOT LIKE '%%@g.us'
                      AND m.key->>'remoteJid' NOT LIKE '%%broadcast%%'
                    ORDER BY m."messageTimestamp" ASC
                    LIMIT %s
                    """,
                    (instance_name, since_ts, limit),
                )
                rows = cur.fetchall()
        records = []
        for key, push_name, message, ts in rows:
            if not isinstance(key, dict):
                continue
            records.append(
                {
                    "key": key,
                    "pushName": push_name,
                    "message": message if isinstance(message, dict) else {},
                    "messageTimestamp": ts,
                }
            )
        return records
    except Exception as exc:
        log.warning("fetch_recent_stored_messages: %s", exc)
        return []

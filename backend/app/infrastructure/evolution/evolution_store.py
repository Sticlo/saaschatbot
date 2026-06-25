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


def fetch_contact_names_index(
    dsn: str, instance_name: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Mapas jid→nombre y teléfono E.164→nombre desde Contact (agenda del celular)."""
    jid_names: dict[str, str] = {}
    phone_names: dict[str, str] = {}
    if not dsn:
        return jid_names, phone_names
    try:
        from app.shared.core.phone import jid_to_phone

        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c."remoteJid", c."pushName"
                    FROM "Contact" c
                    JOIN "Instance" i ON i.id = c."instanceId"
                    WHERE i.name = %s
                      AND c."pushName" IS NOT NULL
                      AND c."pushName" <> ''
                    """,
                    (instance_name,),
                )
                rows = cur.fetchall()
        for jid, push_name in rows:
            if not jid or not push_name:
                continue
            name = str(push_name).strip()
            if not name:
                continue
            jid_names[str(jid)] = name
            if str(jid).endswith("@s.whatsapp.net"):
                phone = jid_to_phone(str(jid))
                if phone:
                    phone_names[phone] = name
    except Exception as exc:
        log.warning("No se pudo leer índice de nombres Contact: %s", exc)
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
    if not dsn:
        return {}
    mappings: dict[str, str] = {}
    try:
        with psycopg.connect(_normalize_dsn(dsn), **_CONNECT_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (m.key->>'remoteJid')
                        m.key->>'remoteJid' AS lid_jid,
                        m.key->>'remoteJidAlt' AS alt_jid
                    FROM "Message" m
                    JOIN "Instance" i ON i.id = m."instanceId"
                    WHERE i.name = %s
                      AND m.key->>'remoteJid' LIKE '%%@lid'
                      AND m.key->>'remoteJidAlt' LIKE '%%@s.whatsapp.net'
                    ORDER BY m.key->>'remoteJid', m."messageTimestamp" DESC
                    """,
                    (instance_name,),
                )
                from app.shared.core.phone import jid_to_phone

                for lid_jid, alt_jid in cur.fetchall():
                    if not lid_jid or not alt_jid:
                        continue
                    phone = jid_to_phone(str(alt_jid))
                    if phone:
                        mappings[str(lid_jid)] = phone
    except Exception as exc:
        log.warning("No se pudo leer mapeos @lid: %s", exc)
    return mappings


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

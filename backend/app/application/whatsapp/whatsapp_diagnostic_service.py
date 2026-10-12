"""Diagnóstico al vincular WhatsApp: cuántos clientes se quedaron sin respuesta, cuánto tarda el
negocio en contestar y qué le preguntan, sacado del historial reciente que WhatsApp manda al
vincular. Es el primer «esto me sirve» del dueño, antes de configurar nada.

Del historial solo se guardan números: ni textos ni teléfonos (el chat va como hash)."""
from __future__ import annotations

import hashlib
import logging
import re
import statistics
import unicodedata
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.application.appointments.appointment_service import active_staff, get_schedule
from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.application.messaging.message_service import (
    _detect_media_type_from_body,
    is_reaction_only,
    parse_messages_upsert,
)
from app.application.results.results_service import is_after_hours
from app.domain.entities import Tenant, WhatsAppSession
from app.domain.entities.enums import WhatsAppStatus
from app.infrastructure.cache.redis_client import get_redis
from app.shared.core.phone import is_owner_jid

log = logging.getLogger(__name__)

WINDOW_DAYS = 30
# El historial llega en lotes durante uno o dos minutos: sin lotes nuevos en este rato, ya llegó.
SETTLE_SECONDS = 25
# Vinculó y WhatsApp no mandó historial (número nuevo): no hay nada que mostrar.
WAIT_FOR_HISTORY_SECONDS = 4 * 60
RAW_TTL_SECONDS = 2 * 24 * 3600
# Un cliente que escribió hace menos de esto puede estar esperando todavía: no se cuenta.
UNANSWERED_MIN_AGE_SECONDS = 30 * 60
SLOW_REPLY_SECONDS = 3600
FAST_REPLY_SECONDS = 5 * 60
MIN_CLIENTS = 3
# Sube cuando cambia qué se cuenta: un diagnóstico viejo no se muestra con reglas nuevas.
DIAGNOSTIC_VERSION = 2
# Muchos números son mixtos: un chat con cientos de mensajes al mes es familia o amigos, no un cliente.
PERSONAL_CHAT_MIN_MESSAGES = 300
# Notificaciones de bancos y tiendas: largas y con links, nunca son un cliente preguntando.
_INQUIRY_MAX_CHARS = 400

TOPICS: tuple[tuple[str, str, re.Pattern], ...] = tuple(
    (key, label, re.compile(pattern))
    for key, label, pattern in (
        (
            "precio",
            "precios",
            r"\b(precio|precios|valor|cuesta|cuestan|costo|cobran|tarifa"
            r"|cuanto (vale|valen|cuesta|cuestan|es|son|sale|salen|cobra|cobran|seria|queda))\b",
        ),
        (
            "envio",
            "envíos y domicilios",
            r"\b(envio|envios|enviar|envian|domicilio|domicilios|despacho|despachan|contraentrega|contra entrega|transportadora)\b",
        ),
        ("ubicacion", "dónde quedan", r"\b(donde (queda|quedan|estan|es)|direccion|ubicacion|ubicados|ubicadas)\b"),
        ("horario", "horarios", r"\b(horario|horarios|a que hora|abren|abierto|abiertos|cierran|atienden|hasta que hora)\b"),
        (
            "disponible",
            "disponibilidad y tallas",
            r"\b(disponible|disponibles|disponibilidad|talla|tallas|stock|catalogo|referencia|referencias|modelos"
            r"|tienen (el|la|los|las|en|disponible|de))\b",
        ),
        ("pago", "formas de pago", r"\b(nequi|daviplata|transferencia|bancolombia|tarjeta|pse|efectivo|pagar|consignar)\b"),
        ("cita", "citas y reservas", r"\b(cita|citas|agendar|agenda|reservar|reserva|turno|cupo|cupos)\b"),
    )
)
_TOPIC_LABELS = {key: label for key, label, _ in TOPICS}
# Respuestas que cierran la charla: si es lo último del chat, nadie quedó esperando.
_CLOSING_WORDS = {
    "ok", "oki", "okay", "okey", "listo", "lista", "dale", "gracias", "muchas", "mil", "perfecto",
    "vale", "bueno", "bien", "super", "genial", "entendido", "ya", "igualmente", "bendiciones",
    "a", "la", "orden", "de", "una", "chevere", "excelente", "jaja", "jajaja", "jajajaja", "q", "que",
}


def _events_key(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    return f"diag:{tenant_id}:{connection_id}:events"


def _last_batch_key(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    return f"diag:{tenant_id}:{connection_id}:last_batch"


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _topics_of(text: str) -> str:
    clean = _normalize(text)
    return ",".join(key for key, _, pattern in TOPICS if pattern.search(clean))


def _is_closing(text: str) -> bool:
    clean = _normalize(text)
    if len(clean) > 40:
        return False
    words = re.findall(r"[a-z]+", clean)
    return all(word in _CLOSING_WORDS for word in words)


_ASKS_RE = re.compile(
    r"\b(cuanto|cuantos|precio|tienen|tiene|hay|manejan|venden|hacen|envian|donde|como (hago|compro|pago|puedo)"
    r"|quisiera|quiero|necesito|info|informacion|me (regala|regalas|das|pasa|pasas|puede|puedes|podria|interesa))\b"
)


def _is_sales_inquiry(text: str, topics: str) -> bool:
    """Alguien preguntando por lo que vende el negocio, no una charla ni una notificación."""
    if not topics or len(text) > _INQUIRY_MAX_CHARS or "http" in text.lower():
        return False
    return "?" in text or bool(_ASKS_RE.search(_normalize(text)))


def _is_noise(body: str) -> bool:
    """Stickers y reacciones: solos no piden respuesta."""
    return is_reaction_only(body) or _detect_media_type_from_body(body) in ("sticker", "reaction")


def record_history_batch(session: WhatsAppSession, data: Any, *, now: Optional[datetime] = None) -> int:
    """Guarda lo mínimo de cada mensaje del historial (quién escribió, cuándo, de qué tema)."""
    connection_id = session.active_connection_id
    if connection_id is None:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)
    owner_jid = session.bound_owner_jid or ""
    owner_phone = session.phone_number or ""
    members: dict[str, float] = {}
    for item in parse_messages_upsert(data):
        jid = str(item.get("remote_jid") or "")
        if not jid.endswith(("@s.whatsapp.net", "@lid")):
            continue
        if is_owner_jid(jid, owner_jid=owner_jid, owner_phone=owner_phone):
            continue
        sent_at = item.get("timestamp")
        body = str(item.get("body") or "")
        if sent_at is None or sent_at < cutoff or sent_at > now + timedelta(minutes=5) or body == "[mensaje]":
            continue
        from_me = bool(item.get("from_me"))
        chat = hashlib.sha1(jid.encode()).hexdigest()[:12]
        if from_me:
            flags = "o||"
        elif _is_noise(body):
            flags = "i|n|"
        elif _is_closing(body):
            flags = "i|c|"
        else:
            topics = _topics_of(body)
            # s = pregunta de venta; m = cualquier otro mensaje que espera respuesta.
            flags = f"i|s|{topics}" if _is_sales_inquiry(body, topics) else "i|m|"
        members[f"{chat}|{flags}|{item.get('message_id')}"] = sent_at.timestamp()
    if not members:
        return 0
    try:
        client = get_redis()
        key = _events_key(session.tenant_id, connection_id)
        pipe = client.pipeline()
        pipe.zadd(key, members)
        pipe.expire(key, RAW_TTL_SECONDS)
        pipe.set(_last_batch_key(session.tenant_id, connection_id), str(now.timestamp()), ex=RAW_TTL_SECONDS)
        pipe.execute()
    except Exception as exc:
        log.debug("diagnóstico: no se pudo guardar el lote tenant=%s: %s", session.tenant_id, exc)
        return 0
    return len(members)


def _working_days(db: Session, tenant_id: uuid.UUID) -> Optional[set[int]]:
    staff = active_staff(db, tenant_id)
    if not staff:
        return None
    return {int(d) for s in staff for d in (s.work_days or [])}


def compute_diagnostic(
    db: Session, tenant: Tenant, connection_id: uuid.UUID, *, now: Optional[datetime] = None
) -> Optional[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    try:
        rows = get_redis().zrange(_events_key(tenant.id, connection_id), 0, -1, withscores=True)
    except Exception:
        return None
    if not rows:
        return None

    profile = get_or_create_tenant_profile(db, tenant.id)
    schedule = get_schedule(profile)
    open_time = time.fromisoformat(schedule.open_time)
    close_time = time.fromisoformat(schedule.close_time)
    working_days = _working_days(db, tenant.id)

    def outside_hours(ts: float) -> bool:
        moment = datetime.fromtimestamp(ts, tz=timezone.utc)
        return is_after_hours(moment, open_time=open_time, close_time=close_time, working_days=working_days)

    chats: dict[str, list[tuple[float, str, str, str]]] = {}
    for raw, score in rows:
        member = raw.decode() if isinstance(raw, bytes) else str(raw)
        parts = member.split("|")
        if len(parts) < 5:
            continue
        chat, direction, kind, topics = parts[0], parts[1], parts[2], parts[3]
        chats.setdefault(chat, []).append((float(score), direction, kind, topics))

    clients = requests = unanswered = after_hours = slow = 0
    waits: list[float] = []
    topic_chats: dict[str, int] = {}
    now_ts = now.timestamp()
    for events in chats.values():
        # Solo cuenta quien preguntó por lo que vende el negocio; las charlas personales no son clientes.
        if len(events) >= PERSONAL_CHAT_MIN_MESSAGES or not any(kind == "s" for _, _, kind, _ in events):
            continue
        events.sort()
        clients += 1
        seen_topics: set[str] = set()
        wrote_after_hours = False
        turn_start: Optional[float] = None
        turn_is_sale = False
        last_message: Optional[float] = None
        for ts, direction, kind, topics in events:
            if direction == "o":
                if turn_start is not None and turn_is_sale:
                    requests += 1
                    wrote_after_hours = wrote_after_hours or outside_hours(turn_start)
                    wait = ts - turn_start
                    waits.append(wait)
                    slow += wait >= SLOW_REPLY_SECONDS
                turn_start = None
                turn_is_sale = False
                last_message = None
                continue
            if kind not in ("s", "m"):
                continue
            turn_start = turn_start if turn_start is not None else ts
            last_message = ts
            if kind == "s":
                turn_is_sale = True
                seen_topics.update(t for t in topics.split(",") if t)
        if turn_start is not None and turn_is_sale:
            requests += 1
            wrote_after_hours = wrote_after_hours or outside_hours(turn_start)
            if last_message is not None and now_ts - last_message >= UNANSWERED_MIN_AGE_SECONDS:
                unanswered += 1
        after_hours += wrote_after_hours
        for topic in seen_topics:
            topic_chats[topic] = topic_chats.get(topic, 0) + 1

    top_topics = [
        {"key": key, "label": _TOPIC_LABELS[key], "chats": count}
        for key, count in sorted(topic_chats.items(), key=lambda kv: -kv[1])
        if count >= 2
    ][:4]
    return {
        "version": DIAGNOSTIC_VERSION,
        "connection_id": str(connection_id),
        "computed_at": now.isoformat(),
        "window_days": WINDOW_DAYS,
        "messages": len(rows),
        "clients": clients,
        "requests": requests,
        "unanswered": unanswered,
        "after_hours": after_hours,
        "median_reply_seconds": round(statistics.median(waits)) if waits else None,
        "fast_reply_share": round(sum(w < FAST_REPLY_SECONDS for w in waits) / len(waits), 2) if waits else None,
        "slow_replies": slow,
        "topics": top_topics,
    }


def _last_batch_at(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> Optional[float]:
    try:
        raw = get_redis().get(_last_batch_key(tenant_id, connection_id))
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return float(raw.decode() if isinstance(raw, bytes) else raw)
    except ValueError:
        return None


def diagnostic_status(db: Session, tenant: Tenant, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """`analyzing` mientras llega el historial, `ready` con los números, `none` si no hay qué mostrar."""
    now = now or datetime.now(timezone.utc)
    profile = get_or_create_tenant_profile(db, tenant.id)
    stored = profile.whatsapp_diagnostic
    session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant.id).first()
    connection_id = session.active_connection_id if session else None

    if stored and stored.get("version") != DIAGNOSTIC_VERSION:
        stored = None
    if connection_id is not None:
        last_batch = _last_batch_at(tenant.id, connection_id)
        current = bool(stored) and stored.get("connection_id") == str(connection_id)
        if last_batch is not None:
            if now.timestamp() - last_batch < SETTLE_SECONDS:
                return {"status": "analyzing"}
            if not current or stored.get("computed_at", "") < datetime.fromtimestamp(last_batch, tz=timezone.utc).isoformat():
                result = compute_diagnostic(db, tenant, connection_id, now=now)
                if result is not None:
                    profile.whatsapp_diagnostic = result
                    db.commit()
                    stored = result
        elif not current and session.status == WhatsAppStatus.CONNECTED.value:
            started = session.connection_started_at
            if started is not None and (now - started).total_seconds() < WAIT_FOR_HISTORY_SECONDS:
                return {"status": "analyzing"}

    if not stored or int(stored.get("clients") or 0) < MIN_CLIENTS:
        return {"status": "none"}
    return {"status": "ready", "diagnostic": stored, "avg_ticket_cop": profile.avg_ticket_cop}

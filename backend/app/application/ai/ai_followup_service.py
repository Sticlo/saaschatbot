"""Seguimiento al cliente que dejó de responder: pasadas las horas que elige el negocio, la IA le
escribe una sola vez retomando la conversación. No insiste más hasta que el cliente vuelva a escribir:
insistir sin respuesta es lo que más reportes de spam genera y pone en riesgo el número del negocio."""

from __future__ import annotations

import json
import logging
import random
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.application.ai.ai_conversation_service import (
    SALES_PLAYBOOK,
    SAFETY_RULES,
    build_system_prompt,
    date_context,
    format_history,
)
from app.application.ai.ai_mode_service import is_classify_only, is_qualify_mode
from app.application.ai.ai_qualify_service import CLOSING_HOLD_REPLIES, build_qualify_system_prompt
from app.application.appointments.appointment_service import BOGOTA
from app.application.orders.order_service import has_recent_purchase
from app.application.whatsapp.messaging_policy import CUSTOMER_SERVICE_WINDOW, as_utc
from app.domain.entities import Conversation, Message, Tenant, TenantProfile
from app.domain.entities.enums import (
    ConversationInterest,
    ConversationMode,
    ConversationStatus,
    MessageDirection,
    MessageSource,
    WhatsAppStatus,
)
from app.infrastructure.ai.ai_text_provider import chat_completion
from app.infrastructure.cache.redis_client import get_redis
from app.infrastructure.persistence.database import SessionLocal

log = logging.getLogger(__name__)

FOLLOWUP_HOURS_CHOICES = (0, 1, 2, 4, 8, 12, 24)
SWEEP_INTERVAL_SECONDS = 5 * 60
# Un silencio más viejo que esto ya no se retoma: revivir chats olvidados es spam.
FOLLOWUP_WINDOW = timedelta(hours=20)
# Nunca de madrugada ni de noche, aunque el horario del negocio diga otra cosa.
EARLIEST = time(8, 0)
LATEST = time(20, 0)
MAX_PER_TENANT_PER_SWEEP = 3
REPLY_RATE_WINDOW = timedelta(days=7)
SWEEP_LOCK_KEY = "ai:followup:sweep"
LOW_REPLY_ALERT_THROTTLE_SECONDS = 24 * 3600
# Varios mensajes salidos en el mismo segundo delatan al bot.
SEND_GAP_SECONDS = (8, 20)
LOCK_TTL_SECONDS = 15 * 60
HISTORY_TURNS = 12
SKIP = "NO"
PURCHASE_QUIET = timedelta(days=3)
# Lo que tarda en salir (la IA lo escribe, espera turno): que no llegue con la ventana de 24 h ya cerrada.
WINDOW_MARGIN = timedelta(minutes=30)

_ENDED_REPLIES = frozenset(CLOSING_HOLD_REPLIES)

FOLLOWUP_INSTRUCTION = """El cliente dejó de responder. Escríbele UN solo mensaje corto (una o dos frases) que lo acerque a comprar, retomando la conversación donde quedó.
- Retoma con algo concreto: lo que estaba mirando, una opción que le sirva según lo que te dijo o el siguiente paso (apartarlo, agendarlo, enviárselo).
- Termina con una pregunta fácil de responder (sí o no, o elegir entre dos opciones). No preguntes «¿sigues interesado?» ni «¿te ayudo con algo más?».
- Sin presionar: no inventes descuentos, urgencia ni cupos limitados.
- No afirmes nada que no esté en la conversación o en la información del negocio: ni que hay existencias, ni su ciudad, ni plazos de entrega.
- No lo saludes como si fuera la primera vez ni repitas lo que ya le dijiste.
- Si la conversación ya terminó (se despidió, ya compró, pagó o agendó, dijo que no le interesa, pidió que no le escriban, quedó esperando que el negocio le confirme algo, o no es un cliente), responde solo: {skip}
Responde únicamente con el texto del mensaje, sin comillas ni JSON."""

_sweeper_thread: Optional[threading.Thread] = None
_sweeper_stop = threading.Event()


def _clock(raw: Optional[str], fallback: time) -> time:
    try:
        hh, mm = (raw or "").split(":")
        return time(int(hh), int(mm))
    except (TypeError, ValueError):
        return fallback


def within_sending_hours(profile: TenantProfile, now: datetime) -> bool:
    start = max(_clock(profile.schedule_open_time, EARLIEST), EARLIEST)
    end = min(_clock(profile.schedule_close_time, LATEST), LATEST)
    if start >= end:
        start, end = EARLIEST, LATEST
    return start <= now.astimezone(BOGOTA).time() < end


def _last_message(db: Session, conversation_id, direction: Optional[str] = None) -> Optional[Message]:
    query = db.query(Message).filter(Message.conversation_id == conversation_id)
    if direction:
        query = query.filter(Message.direction == direction)
    return query.order_by(Message.created_at.desc()).first()


def due_followups(db: Session, tenant: Tenant, now: datetime) -> list[Conversation]:
    """Chats donde la IA habló de último hace más de las horas elegidas y el cliente no contestó."""
    from app.application.ai.ai_service import _awaiting_human_since, should_ai_respond
    from app.application.conversations.interest_alert_service import is_alert_phone

    hours = tenant.profile.followup_hours
    latest = now - timedelta(hours=hours)
    query = db.query(Conversation).filter(
        Conversation.tenant_id == tenant.id,
        Conversation.ai_active.is_(True),
        Conversation.mode == ConversationMode.AUTO.value,
        Conversation.status != ConversationStatus.EXCLUDED.value,
        Conversation.is_archived.is_(False),
        or_(
            Conversation.interest_status.is_(None),
            Conversation.interest_status != ConversationInterest.NOT_INTERESTED.value,
        ),
        Conversation.last_message_at <= latest,
        Conversation.last_message_at >= latest - FOLLOWUP_WINDOW,
    )
    if not tenant.ai_global_enabled:
        query = query.filter(Conversation.ai_override_global.is_(True))

    due: list[Conversation] = []
    for conversation in query.order_by(Conversation.last_message_at.asc()).all():
        last = _last_message(db, conversation.id)
        if last is None or last.direction != MessageDirection.OUT.value or last.source != MessageSource.BOT.value:
            continue
        if last.created_at > latest or last.created_at < latest - FOLLOWUP_WINDOW:
            continue
        last_in = _last_message(db, conversation.id, MessageDirection.IN.value)
        if last_in is None or now - as_utc(last_in.created_at) > CUSTOMER_SERVICE_WINDOW - WINDOW_MARGIN:
            continue
        if conversation.followup_sent_at and conversation.followup_sent_at >= last_in.created_at:
            continue
        if (last.body or "").strip() in _ENDED_REPLIES:
            continue
        if not should_ai_respond(tenant, conversation) or is_alert_phone(tenant, conversation.contact_phone):
            continue
        if _awaiting_human_since(conversation.id) is not None:
            continue
        # Ya compró (o se le avisó el despacho): insistirle sería venderle lo mismo otra vez.
        if has_recent_purchase(db, conversation.id, now - PURCHASE_QUIET):
            continue
        due.append(conversation)
    return due


def _daily_key(tenant_id, now: datetime) -> str:
    return f"ai:followup:sent:{tenant_id}:{now.astimezone(BOGOTA):%Y%m%d}"


def sent_today(tenant_id, now: datetime) -> int:
    try:
        return int(get_redis().get(_daily_key(tenant_id, now)) or 0)
    except Exception:
        return 0


def _count_sent(tenant_id, now: datetime) -> None:
    key = _daily_key(tenant_id, now)
    client = get_redis()
    client.incr(key)
    client.expire(key, 2 * 24 * 3600)


def reply_rate(db: Session, tenant_id, now: datetime) -> tuple[int, int]:
    """(seguimientos, cuántos respondieron) de los últimos 7 días."""
    replied = (
        db.query(Message.id)
        .filter(
            Message.conversation_id == Conversation.id,
            Message.direction == MessageDirection.IN.value,
            Message.created_at > Conversation.followup_sent_at,
        )
        .exists()
    )
    rows = (
        db.query(Conversation.id, replied)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.followup_sent_at >= now - REPLY_RATE_WINDOW,
        )
        .all()
    )
    return len(rows), sum(1 for _, answered in rows if answered)


def followups_paused_for_low_replies(db: Session, tenant: Tenant, now: datetime) -> bool:
    """Si casi nadie contesta los seguimientos, WhatsApp los lee como spam: se pausan solos."""
    sent, answered = reply_rate(db, tenant.id, now)
    if sent < settings.followup_min_sample or answered / sent >= settings.followup_min_reply_rate:
        return False
    from app.application.monitoring.dev_alerts import alert_dev

    alert_dev(
        f"followup:low_reply:{tenant.id}",
        f"Seguimientos pausados: {tenant.business_name} casi no recibe respuesta",
        f"En 7 días respondieron {answered} de {sent} seguimientos ({answered / sent:.0%}). "
        "Seguir escribiendo a quien no contesta puede hacer que WhatsApp restrinja el número. "
        "Vuelven a salir solos cuando esos 7 días queden atrás.",
        severity="warning",
        throttle_seconds=LOW_REPLY_ALERT_THROTTLE_SECONDS,
    )
    return True


def _clean(raw: str) -> Optional[str]:
    text = (raw or "").strip()
    if text.startswith("{"):
        try:
            text = str(json.loads(text).get("message") or "").strip()
        except (ValueError, AttributeError):
            return None
    text = text.strip().strip('"«»“”').strip()
    if not text or text.rstrip(".! ").upper() == SKIP:
        return None
    return text


def generate_followup(tenant: Tenant, conversation: Conversation, history: list[Message]) -> Optional[str]:
    """El texto del seguimiento, o None si la IA ve que la conversación ya terminó."""
    profile = tenant.profile
    if is_qualify_mode(profile):
        system = build_qualify_system_prompt(tenant, profile)
    else:
        system = build_system_prompt(tenant, profile)
    name = conversation.contact_name or ""
    if name and not name.startswith("+"):
        system += f"\n\nNombre del contacto: {name}"
    messages = [
        {"role": "system", "content": system + SALES_PLAYBOOK + date_context() + SAFETY_RULES},
        *format_history(history, limit=HISTORY_TURNS),
        {
            "role": "system",
            "content": FOLLOWUP_INSTRUCTION.format(skip=SKIP),
        },
    ]
    return _clean(chat_completion(messages, temperature=0.6, max_tokens=160))


def send_followup(db: Session, tenant: Tenant, conversation: Conversation, now: datetime) -> bool:
    from app.application.whatsapp.whatsapp_service import send_text_message

    if not get_redis().set(f"ai:followup:{conversation.id}", "1", nx=True, ex=LOCK_TTL_SECONDS):
        return False
    history = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.asc())
        .all()
    )
    text = generate_followup(tenant, conversation, history)
    conversation.followup_sent_at = now
    if text is None:
        db.commit()
        log.info("Seguimiento omitido: la conversación ya terminó conv=%s", conversation.id)
        return False
    send_text_message(
        db,
        tenant=tenant,
        session=tenant.whatsapp_session,
        conversation=conversation,
        text=text,
        source=MessageSource.BOT.value,
    )
    db.commit()
    try:
        _count_sent(tenant.id, now)
    except Exception:
        log.debug("No se pudo contar el seguimiento tenant=%s", tenant.id, exc_info=True)
    log.info("Seguimiento enviado conv=%s", conversation.id)
    return True


def _tenants_with_followup(db: Session) -> list[Tenant]:
    return (
        db.query(Tenant)
        .join(TenantProfile, TenantProfile.tenant_id == Tenant.id)
        .filter(
            Tenant.is_active.is_(True),
            Tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value,
            TenantProfile.followup_hours > 0,
        )
        .all()
    )


def sweep_once(now: Optional[datetime] = None, *, gap=SEND_GAP_SECONDS) -> int:
    if not settings.whatsapp_proactive_messages:
        return 0
    now = now or datetime.now(timezone.utc)
    sent = 0
    db = SessionLocal()
    try:
        for tenant in _tenants_with_followup(db):
            if _sweeper_stop.is_set():
                break
            profile = tenant.profile
            if tenant.whatsapp_session is None or is_classify_only(profile):
                continue
            if not within_sending_hours(profile, now):
                continue
            room = min(MAX_PER_TENANT_PER_SWEEP, settings.followup_daily_cap - sent_today(tenant.id, now))
            if room <= 0 or followups_paused_for_low_replies(db, tenant, now):
                continue
            for conversation in due_followups(db, tenant, now)[:room]:
                try:
                    if send_followup(db, tenant, conversation, now):
                        sent += 1
                        if gap and _sweeper_stop.wait(random.uniform(*gap)):
                            return sent
                except Exception:
                    db.rollback()
                    log.exception("No se pudo enviar el seguimiento conv=%s", conversation.id)
    finally:
        db.close()
    return sent


def _sweeper_loop() -> None:
    log.info("Follow-up sweeper started")
    while not _sweeper_stop.wait(SWEEP_INTERVAL_SECONDS):
        try:
            # Cada worker de IA arranca este hilo: sin el candado, cada réplica sumaría su propio cupo.
            if not get_redis().set(SWEEP_LOCK_KEY, "1", nx=True, ex=SWEEP_INTERVAL_SECONDS - 30):
                continue
            sweep_once()
        except Exception:
            log.exception("Follow-up sweeper error")
    log.info("Follow-up sweeper stopped")


def start_followup_sweeper() -> None:
    global _sweeper_thread
    if _sweeper_thread and _sweeper_thread.is_alive():
        return
    _sweeper_stop.clear()
    _sweeper_thread = threading.Thread(target=_sweeper_loop, name="ai-followup-sweeper", daemon=True)
    _sweeper_thread.start()


def stop_followup_sweeper() -> None:
    _sweeper_stop.set()
    if _sweeper_thread and _sweeper_thread.is_alive():
        _sweeper_thread.join(timeout=1)

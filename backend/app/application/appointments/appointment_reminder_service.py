"""Recordatorio de la cita por WhatsApp 2 horas antes: menos clientes que se olvidan y no llegan.

Solo se le escribe a quien ya habló con el negocio por WhatsApp (nunca un mensaje en frío), una sola
vez por cita, y nunca de madrugada ni de noche."""

from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.application.appointments.appointment_service import BOGOTA
from app.domain.entities import Appointment, Conversation, Tenant, TenantProfile
from app.domain.entities.enums import ConversationStatus, MessageSource, WhatsAppStatus
from app.infrastructure.cache.redis_client import get_redis
from app.infrastructure.persistence.database import SessionLocal
from app.shared.core.phone import normalize_phone

log = logging.getLogger(__name__)

REMIND_BEFORE = timedelta(hours=2)
# Si la cita ya casi empieza, el recordatorio llega tarde y solo estorba.
TOO_CLOSE = timedelta(minutes=20)
SWEEP_INTERVAL_SECONDS = 5 * 60
EARLIEST = time(7, 0)
LATEST = time(21, 0)
LOCK_TTL_SECONDS = 15 * 60
SEND_GAP_SECONDS = (4, 10)

_sweeper_thread: Optional[threading.Thread] = None
_sweeper_stop = threading.Event()


def _clock(moment: datetime) -> str:
    local = moment.astimezone(BOGOTA)
    suffix = "a. m." if local.hour < 12 else "p. m."
    return f"{local.hour % 12 or 12}:{local.minute:02d} {suffix}"


def _first_name(name: str) -> str:
    first = (name or "").strip().split(" ")[0]
    return "" if not first or first.startswith("+") or first.isdigit() else first


def reminder_text(tenant: Tenant, appointment: Appointment, now: datetime) -> str:
    name = _first_name(appointment.client_name)
    greeting = f"Hola {name}" if name else "Hola"
    same_day = appointment.starts_at.astimezone(BOGOTA).date() == now.astimezone(BOGOTA).date()
    when = f"hoy a las {_clock(appointment.starts_at)}" if same_day else f"a las {_clock(appointment.starts_at)}"
    staff = f" con {appointment.staff.name}" if appointment.staff and appointment.staff.name else ""
    return (
        f"{greeting}, te recordamos tu cita {when}{staff} en {tenant.business_name}. "
        "Si necesitas cambiarla, respóndenos por aquí."
    )


def within_reminder_hours(now: datetime) -> bool:
    return EARLIEST <= now.astimezone(BOGOTA).time() < LATEST


def _client_chat(db: Session, tenant: Tenant, appointment: Appointment) -> Optional[Conversation]:
    """El chat donde el cliente ya le escribió al negocio; sin chat, no hay recordatorio."""
    from app.application.conversations.interest_alert_service import is_alert_phone

    conversation = None
    if appointment.conversation_id:
        conversation = db.get(Conversation, appointment.conversation_id)
    if conversation is None and appointment.client_phone:
        phone = normalize_phone(appointment.client_phone)
        if phone:
            conversation = (
                db.query(Conversation)
                .filter(Conversation.tenant_id == tenant.id, Conversation.contact_phone == phone)
                .order_by(Conversation.last_message_at.desc().nullslast())
                .first()
            )
    if conversation is None or conversation.tenant_id != tenant.id:
        return None
    if conversation.status == ConversationStatus.EXCLUDED.value:
        return None
    if is_alert_phone(tenant, conversation.contact_phone):
        return None
    return conversation


def due_reminders(db: Session, tenant: Tenant, now: datetime) -> list[Appointment]:
    return (
        db.query(Appointment)
        .filter(
            Appointment.tenant_id == tenant.id,
            Appointment.reminder_sent_at.is_(None),
            Appointment.starts_at > now + TOO_CLOSE,
            Appointment.starts_at <= now + REMIND_BEFORE,
            # Recién agendada (menos de 2 h antes): acaba de hablar con el negocio, no hace falta.
            Appointment.created_at <= Appointment.starts_at - REMIND_BEFORE,
        )
        .order_by(Appointment.starts_at.asc())
        .all()
    )


def send_reminder(db: Session, tenant: Tenant, appointment: Appointment, now: datetime) -> bool:
    from app.application.whatsapp.whatsapp_service import send_text_message

    conversation = _client_chat(db, tenant, appointment)
    if conversation is None:
        return False
    if not get_redis().set(f"appt:reminder:{appointment.id}", "1", nx=True, ex=LOCK_TTL_SECONDS):
        return False
    appointment.reminder_sent_at = now
    # Es un aviso, no una venta: que el seguimiento automático no le escriba después por esto.
    conversation.followup_sent_at = now
    db.flush()
    send_text_message(
        db,
        tenant=tenant,
        session=tenant.whatsapp_session,
        conversation=conversation,
        text=reminder_text(tenant, appointment, now),
        source=MessageSource.BOT.value,
    )
    db.commit()
    log.info("Recordatorio de cita enviado appt=%s conv=%s", appointment.id, conversation.id)
    return True


def _tenants_with_reminders(db: Session) -> list[Tenant]:
    return (
        db.query(Tenant)
        .join(TenantProfile, TenantProfile.tenant_id == Tenant.id)
        .filter(
            Tenant.is_active.is_(True),
            Tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value,
            TenantProfile.appointment_reminders.is_(True),
        )
        .all()
    )


def sweep_once(now: Optional[datetime] = None, *, gap=SEND_GAP_SECONDS) -> int:
    now = now or datetime.now(timezone.utc)
    if not within_reminder_hours(now):
        return 0
    sent = 0
    db = SessionLocal()
    try:
        for tenant in _tenants_with_reminders(db):
            if _sweeper_stop.is_set():
                break
            if tenant.whatsapp_session is None:
                continue
            for appointment in due_reminders(db, tenant, now):
                try:
                    if send_reminder(db, tenant, appointment, now):
                        sent += 1
                        if gap and _sweeper_stop.wait(random.uniform(*gap)):
                            return sent
                except Exception:
                    db.rollback()
                    log.exception("No se pudo enviar el recordatorio appt=%s", appointment.id)
    finally:
        db.close()
    return sent


def _sweeper_loop() -> None:
    log.info("Appointment reminder sweeper started")
    while not _sweeper_stop.wait(SWEEP_INTERVAL_SECONDS):
        try:
            sweep_once()
        except Exception:
            log.exception("Appointment reminder sweeper error")
    log.info("Appointment reminder sweeper stopped")


def start_reminder_sweeper() -> None:
    global _sweeper_thread
    if _sweeper_thread and _sweeper_thread.is_alive():
        return
    _sweeper_stop.clear()
    _sweeper_thread = threading.Thread(target=_sweeper_loop, name="appointment-reminder-sweeper", daemon=True)
    _sweeper_thread.start()


def stop_reminder_sweeper() -> None:
    _sweeper_stop.set()
    if _sweeper_thread and _sweeper_thread.is_alive():
        _sweeper_thread.join(timeout=1)

"""Resumen de la mañana: lo que la IA atendió mientras el dueño descansaba y lo que tiene hoy."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.application.appointments.appointment_service import BOGOTA
from app.domain.entities import Appointment, Conversation, Message
from app.domain.entities.enums import ConversationInterest, MessageDirection, MessageSource

# «Anoche» empieza cuando el negocio suele cerrar.
NIGHT_STARTS = time(19, 0)


def _clock(moment: datetime) -> str:
    local = moment.astimezone(BOGOTA)
    suffix = "a. m." if local.hour < 12 else "p. m."
    return f"{local.hour % 12 or 12}:{local.minute:02d} {suffix}"


def morning_summary(db: Session, tenant_id, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(BOGOTA).date()
    since = datetime.combine(today - timedelta(days=1), NIGHT_STARTS, tzinfo=BOGOTA)
    day_start = datetime.combine(today, time.min, tzinfo=BOGOTA)
    day_end = day_start + timedelta(days=1)

    def _chats(*conditions):
        return db.query(Message.conversation_id).filter(
            Message.tenant_id == tenant_id, Message.created_at >= since, Message.created_at <= now, *conditions
        )

    # Atendido = el cliente escribió y la IA le contestó; los recordatorios y seguimientos no cuentan.
    attended = {
        row[0]
        for row in _chats(Message.direction == MessageDirection.OUT.value, Message.source == MessageSource.BOT.value)
        .intersect(_chats(Message.direction == MessageDirection.IN.value))
        .all()
    }
    interested_count = (
        db.query(func.count(Conversation.id))
        .filter(
            Conversation.id.in_(attended),
            Conversation.interest_status == ConversationInterest.INTERESTED.value,
        )
        .scalar()
        if attended
        else 0
    )
    appointments = (
        db.query(Appointment.starts_at)
        .filter(
            Appointment.tenant_id == tenant_id,
            Appointment.starts_at >= day_start,
            Appointment.starts_at < day_end,
        )
        .order_by(Appointment.starts_at.asc())
        .all()
    )
    upcoming = [row.starts_at for row in appointments if row.starts_at >= now]
    return {
        "date": today.isoformat(),
        "attended": len(attended),
        "interested": interested_count,
        "appointments_today": len(appointments),
        "next_appointment_at": _clock(upcoming[0]) if upcoming else None,
    }

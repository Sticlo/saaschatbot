from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.domain.entities import Appointment, TenantProfile

BOGOTA = ZoneInfo("America/Bogota")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass
class ScheduleSettings:
    open_time: str
    close_time: str
    slot_minutes: int


def _parse_hhmm(value: str) -> time:
    match = _TIME_RE.match((value or "").strip())
    if not match:
        raise ValueError("Hora inválida (usa HH:MM)")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("Hora inválida")
    return time(hour=hour, minute=minute)


def _format_hhmm(value: time) -> str:
    return value.strftime("%H:%M")


def _combine_local(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=BOGOTA)


def get_schedule(profile: TenantProfile) -> ScheduleSettings:
    slot = int(profile.schedule_slot_minutes or 60)
    slot = max(15, min(slot, 240))
    return ScheduleSettings(
        open_time=profile.schedule_open_time or "08:00",
        close_time=profile.schedule_close_time or "18:00",
        slot_minutes=slot,
    )


def update_schedule(
    profile: TenantProfile,
    *,
    open_time: str,
    close_time: str,
    slot_minutes: int,
) -> ScheduleSettings:
    open_clock = _parse_hhmm(open_time)
    close_clock = _parse_hhmm(close_time)
    if close_clock <= open_clock:
        raise ValueError("La hora de cierre debe ser después de la apertura")
    slot = max(15, min(int(slot_minutes), 240))
    profile.schedule_open_time = _format_hhmm(open_clock)
    profile.schedule_close_time = _format_hhmm(close_clock)
    profile.schedule_slot_minutes = slot
    return get_schedule(profile)


def _slot_ranges(day: date, schedule: ScheduleSettings) -> list[tuple[datetime, datetime]]:
    start = _combine_local(day, _parse_hhmm(schedule.open_time))
    end = _combine_local(day, _parse_hhmm(schedule.close_time))
    step = timedelta(minutes=schedule.slot_minutes)
    slots: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor + step <= end:
        slots.append((cursor, cursor + step))
        cursor += step
    return slots


def _appointment_to_dict(row: Appointment) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "conversation_id": str(row.conversation_id) if row.conversation_id else None,
        "starts_at": row.starts_at.isoformat(),
        "ends_at": row.ends_at.isoformat(),
        "client_name": row.client_name or "",
        "client_phone": row.client_phone,
        "notes": row.notes or "",
    }


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def build_day_view(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    day: date,
) -> dict[str, Any]:
    profile = get_or_create_tenant_profile(db, tenant_id)
    schedule = get_schedule(profile)
    day_start = _combine_local(day, time.min)
    day_end = _combine_local(day, time.max)

    rows = (
        db.query(Appointment)
        .filter(
            Appointment.tenant_id == tenant_id,
            Appointment.starts_at < day_end,
            Appointment.ends_at > day_start,
        )
        .order_by(Appointment.starts_at.asc())
        .all()
    )

    slots: list[dict[str, Any]] = []
    for slot_start, slot_end in _slot_ranges(day, schedule):
        match = None
        for row in rows:
            if _overlaps(slot_start, slot_end, row.starts_at, row.ends_at):
                match = row
                break
        if match:
            label = match.client_name.strip() or "Cliente"
            slots.append(
                {
                    "start": _format_hhmm(slot_start.astimezone(BOGOTA).time()),
                    "end": _format_hhmm(slot_end.astimezone(BOGOTA).time()),
                    "status": "busy",
                    "appointment": _appointment_to_dict(match),
                }
            )
        else:
            slots.append(
                {
                    "start": _format_hhmm(slot_start.astimezone(BOGOTA).time()),
                    "end": _format_hhmm(slot_end.astimezone(BOGOTA).time()),
                    "status": "free",
                    "appointment": None,
                }
            )

    return {
        "date": day.isoformat(),
        "schedule": {
            "open_time": schedule.open_time,
            "close_time": schedule.close_time,
            "slot_minutes": schedule.slot_minutes,
        },
        "slots": slots,
    }


def create_appointment(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    starts_at: datetime,
    ends_at: datetime,
    client_name: str,
    client_phone: Optional[str] = None,
    notes: Optional[str] = None,
    conversation_id: Optional[uuid.UUID] = None,
) -> Appointment:
    if ends_at <= starts_at:
        raise ValueError("La hora de fin debe ser después del inicio")
    if not client_name.strip():
        raise ValueError("Indica el nombre del cliente")

    conflict = (
        db.query(Appointment.id)
        .filter(
            Appointment.tenant_id == tenant_id,
            Appointment.starts_at < ends_at,
            Appointment.ends_at > starts_at,
        )
        .first()
    )
    if conflict:
        raise ValueError("Ese horario ya está ocupado")

    row = Appointment(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        starts_at=starts_at.astimezone(timezone.utc),
        ends_at=ends_at.astimezone(timezone.utc),
        client_name=client_name.strip()[:255],
        client_phone=(client_phone or "").strip()[:64] or None,
        notes=(notes or "").strip() or None,
    )
    db.add(row)
    db.flush()
    return row


def update_appointment(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    appointment_id: uuid.UUID,
    client_name: Optional[str] = None,
    client_phone: Optional[str] = None,
    notes: Optional[str] = None,
    starts_at: Optional[datetime] = None,
    ends_at: Optional[datetime] = None,
) -> Appointment:
    row = (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.tenant_id == tenant_id)
        .first()
    )
    if row is None:
        raise ValueError("Cita no encontrada")

    new_start = starts_at.astimezone(timezone.utc) if starts_at else row.starts_at
    new_end = ends_at.astimezone(timezone.utc) if ends_at else row.ends_at
    if new_end <= new_start:
        raise ValueError("La hora de fin debe ser después del inicio")

    conflict = (
        db.query(Appointment.id)
        .filter(
            Appointment.tenant_id == tenant_id,
            Appointment.id != appointment_id,
            Appointment.starts_at < new_end,
            Appointment.ends_at > new_start,
        )
        .first()
    )
    if conflict:
        raise ValueError("Ese horario ya está ocupado")

    if client_name is not None:
        if not client_name.strip():
            raise ValueError("Indica el nombre del cliente")
        row.client_name = client_name.strip()[:255]
    if client_phone is not None:
        row.client_phone = client_phone.strip()[:64] or None
    if notes is not None:
        row.notes = notes.strip() or None
    row.starts_at = new_start
    row.ends_at = new_end
    db.flush()
    return row


def delete_appointment(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    appointment_id: uuid.UUID,
) -> None:
    row = (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.tenant_id == tenant_id)
        .first()
    )
    if row is None:
        raise ValueError("Cita no encontrada")
    db.delete(row)
    db.flush()


def parse_slot_to_datetimes(day: date, start_hhmm: str, end_hhmm: str) -> tuple[datetime, datetime]:
    return _combine_local(day, _parse_hhmm(start_hhmm)), _combine_local(day, _parse_hhmm(end_hhmm))


def collect_upcoming_free_slots(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    days: int = 4,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Próximos bloques libres para que la IA los ofrezca."""
    from datetime import datetime

    today = datetime.now(BOGOTA).date()
    out: list[dict[str, Any]] = []
    for offset in range(max(1, days)):
        day = today + timedelta(days=offset)
        view = build_day_view(db, tenant_id=tenant_id, day=day)
        day_label = day.strftime("%Y-%m-%d")
        human_day = _human_day_label(day, today)
        for slot in view.get("slots") or []:
            if slot.get("status") != "free":
                continue
            start = str(slot.get("start") or "")
            end = str(slot.get("end") or "")
            out.append(
                {
                    "date": day_label,
                    "start": start,
                    "end": end,
                    "label": f"{human_day} {start}–{end}",
                }
            )
            if len(out) >= limit:
                return out
    return out


def _human_day_label(day: date, today: date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "Hoy"
    if delta == 1:
        return "Mañana"
    names = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
    return names[day.weekday()]


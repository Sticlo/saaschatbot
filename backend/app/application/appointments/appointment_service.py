from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.domain.entities import Appointment, StaffMember, TenantProfile

BOGOTA = ZoneInfo("America/Bogota")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
DEFAULT_STAFF_LABEL = "profesional"


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


def staff_label(profile: Optional[TenantProfile]) -> str:
    return ((profile.staff_label if profile else "") or "").strip() or DEFAULT_STAFF_LABEL


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


# --- Equipo -------------------------------------------------------------------------------


def all_staff(db: Session, tenant_id: uuid.UUID) -> list[StaffMember]:
    return (
        db.query(StaffMember)
        .filter(StaffMember.tenant_id == tenant_id)
        .order_by(StaffMember.position.asc(), StaffMember.created_at.asc())
        .all()
    )


def active_staff(db: Session, tenant_id: uuid.UUID) -> list[StaffMember]:
    return [s for s in all_staff(db, tenant_id) if s.is_active]


def staff_window(staff: StaffMember, schedule: ScheduleSettings, day: date) -> Optional[tuple[time, time]]:
    """Horas en que atiende ese día, dentro del horario del negocio. None = no trabaja."""
    if day.weekday() not in (staff.work_days or []):
        return None
    start = _parse_hhmm(schedule.open_time)
    end = _parse_hhmm(schedule.close_time)
    if staff.start_time:
        start = max(start, _parse_hhmm(staff.start_time))
    if staff.end_time:
        end = min(end, _parse_hhmm(staff.end_time))
    return (start, end) if end > start else None


def _staff_works(staff: StaffMember, schedule: ScheduleSettings, starts_at: datetime, ends_at: datetime) -> bool:
    local_start = starts_at.astimezone(BOGOTA)
    local_end = ends_at.astimezone(BOGOTA)
    if local_end.date() != local_start.date():
        return False
    window = staff_window(staff, schedule, local_start.date())
    return bool(window and window[0] <= local_start.time() and local_end.time() <= window[1])


def staff_to_dict(staff: StaffMember) -> dict[str, Any]:
    return {
        "id": str(staff.id),
        "name": staff.name,
        "phone": staff.phone or "",
        "is_active": bool(staff.is_active),
        "work_days": sorted(int(d) for d in (staff.work_days or [])),
        "start_time": staff.start_time or "",
        "end_time": staff.end_time or "",
    }


# --- Vista del día y horarios libres ------------------------------------------------------


def _appointment_to_dict(row: Appointment) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "conversation_id": str(row.conversation_id) if row.conversation_id else None,
        "staff_id": str(row.staff_id) if row.staff_id else None,
        "staff_name": row.staff.name if row.staff else "",
        "starts_at": row.starts_at.isoformat(),
        "ends_at": row.ends_at.isoformat(),
        "client_name": row.client_name or "",
        "client_phone": row.client_phone,
        "notes": row.notes or "",
    }


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _day_appointments(db: Session, tenant_id: uuid.UUID, day: date) -> list[Appointment]:
    day_start = _combine_local(day, time.min)
    day_end = _combine_local(day, time.max)
    return (
        db.query(Appointment)
        .filter(
            Appointment.tenant_id == tenant_id,
            Appointment.starts_at < day_end,
            Appointment.ends_at > day_start,
        )
        .order_by(Appointment.starts_at.asc())
        .all()
    )


def _slot_dict(slot_start: datetime, slot_end: datetime, match: Optional[Appointment]) -> dict[str, Any]:
    return {
        "start": _format_hhmm(slot_start.astimezone(BOGOTA).time()),
        "end": _format_hhmm(slot_end.astimezone(BOGOTA).time()),
        "status": "busy" if match else "free",
        "appointment": _appointment_to_dict(match) if match else None,
    }


def build_day_view(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    day: date,
    staff_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    profile = get_or_create_tenant_profile(db, tenant_id)
    schedule = get_schedule(profile)
    rows = _day_appointments(db, tenant_id, day)
    team = all_staff(db, tenant_id)
    selected = next((s for s in team if s.id == staff_id), None) or next(
        (s for s in team if s.is_active), team[0] if team else None
    )

    slots: list[dict[str, Any]] = []
    off_day = False
    if selected is None:
        for slot_start, slot_end in _slot_ranges(day, schedule):
            match = next((r for r in rows if _overlaps(slot_start, slot_end, r.starts_at, r.ends_at)), None)
            slots.append(_slot_dict(slot_start, slot_end, match))
    else:
        own = [r for r in rows if r.staff_id == selected.id]
        window = staff_window(selected, schedule, day)
        off_day = window is None
        for slot_start, slot_end in _slot_ranges(day, schedule):
            match = next((r for r in own if _overlaps(slot_start, slot_end, r.starts_at, r.ends_at)), None)
            local = slot_start.astimezone(BOGOTA).time()
            local_end = slot_end.astimezone(BOGOTA).time()
            works = bool(window and window[0] <= local and local_end <= window[1])
            if works or match:
                slots.append(_slot_dict(slot_start, slot_end, match))

    return {
        "date": day.isoformat(),
        "schedule": {
            "open_time": schedule.open_time,
            "close_time": schedule.close_time,
            "slot_minutes": schedule.slot_minutes,
            "ai_booking_enabled": bool(profile.ai_booking_enabled),
            "staff_label": profile.staff_label or "",
        },
        "staff": [staff_to_dict(s) for s in team],
        "staff_id": str(selected.id) if selected else None,
        "off_day": off_day,
        "slots": slots,
    }


def _ordered_free_staff(
    staff: list[StaffMember],
    rows: list[Appointment],
    schedule: ScheduleSettings,
    starts_at: datetime,
    ends_at: datetime,
) -> list[StaffMember]:
    """Libres a esa hora, primero quien tenga menos citas ese día (reparto parejo)."""
    load = {s.id: 0 for s in staff}
    for row in rows:
        if row.staff_id in load:
            load[row.staff_id] += 1
    free = [
        s
        for s in staff
        if _staff_works(s, schedule, starts_at, ends_at)
        and not any(
            r.staff_id == s.id and _overlaps(starts_at, ends_at, r.starts_at, r.ends_at) for r in rows
        )
    ]
    order = {s.id: i for i, s in enumerate(staff)}
    return sorted(free, key=lambda s: (load[s.id], order[s.id]))


def free_staff_for_slot(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    starts_at: datetime,
    ends_at: datetime,
    preferred_staff_id: Optional[uuid.UUID] = None,
) -> list[StaffMember]:
    profile = get_or_create_tenant_profile(db, tenant_id)
    staff = active_staff(db, tenant_id)
    if preferred_staff_id is not None:
        staff = [s for s in staff if s.id == preferred_staff_id]
    rows = _day_appointments(db, tenant_id, starts_at.astimezone(BOGOTA).date())
    return _ordered_free_staff(staff, rows, get_schedule(profile), starts_at, ends_at)


BOOKING_LEAD_MINUTES = 30


def collect_upcoming_free_slots(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    days: int = 4,
    limit: Optional[int] = 12,
    now: Optional[datetime] = None,
    staff_id: Optional[uuid.UUID] = None,
    extra_days: Iterable[date] = (),
) -> list[dict[str, Any]]:
    """Próximos bloques libres para que la IA los ofrezca. Con equipo, cada bloque trae
    quiénes están libres (`staff`), en orden de reparto. `extra_days`: fechas que pidió el
    cliente aunque estén más allá de `days`. limit=None no recorta."""
    current = (now or datetime.now(BOGOTA)).astimezone(BOGOTA)
    earliest = current + timedelta(minutes=BOOKING_LEAD_MINUTES)
    today = current.date()
    day_list = sorted(
        {today + timedelta(days=offset) for offset in range(max(1, days))}
        | {d for d in extra_days if d >= today}
    )
    profile = get_or_create_tenant_profile(db, tenant_id)
    schedule = get_schedule(profile)
    staff = active_staff(db, tenant_id)
    team_mode = bool(all_staff(db, tenant_id))
    if staff_id is not None:
        staff = [s for s in staff if s.id == staff_id]

    out: list[dict[str, Any]] = []
    for day in day_list:
        rows = _day_appointments(db, tenant_id, day)
        human_day = _human_day_label(day, today)
        for slot_start, slot_end in _slot_ranges(day, schedule):
            if slot_start < earliest:
                continue
            start = _format_hhmm(slot_start.time())
            end = _format_hhmm(slot_end.time())
            entry: dict[str, Any] = {
                "date": day.isoformat(),
                "start": start,
                "end": end,
                "label": f"{human_day} {start}–{end}",
            }
            if team_mode:
                free = _ordered_free_staff(staff, rows, schedule, slot_start, slot_end)
                if not free:
                    continue
                entry["staff"] = [{"id": str(s.id), "name": s.name} for s in free]
            elif any(_overlaps(slot_start, slot_end, r.starts_at, r.ends_at) for r in rows):
                continue
            out.append(entry)
            if limit is not None and len(out) >= limit:
                return out
    return out


# --- Crear, editar y borrar citas ---------------------------------------------------------


def _lock_staff(db: Session, tenant_id: uuid.UUID, staff_id: uuid.UUID) -> StaffMember:
    staff = (
        db.query(StaffMember)
        .filter(StaffMember.id == staff_id, StaffMember.tenant_id == tenant_id)
        .with_for_update()
        .first()
    )
    if staff is None:
        raise ValueError("Esa persona no está en tu equipo")
    return staff


def _has_conflict(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    starts_at: datetime,
    ends_at: datetime,
    staff_id: Optional[uuid.UUID],
    exclude_id: Optional[uuid.UUID] = None,
) -> bool:
    query = db.query(Appointment.id).filter(
        Appointment.tenant_id == tenant_id,
        Appointment.starts_at < ends_at,
        Appointment.ends_at > starts_at,
    )
    if staff_id is not None:
        query = query.filter(Appointment.staff_id == staff_id)
    if exclude_id is not None:
        query = query.filter(Appointment.id != exclude_id)
    return query.first() is not None


def _reserve_staff(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    starts_at: datetime,
    ends_at: datetime,
    staff_id: Optional[uuid.UUID],
) -> Optional[uuid.UUID]:
    """Devuelve el empleado que queda con la cita (None = negocio sin equipo).

    Bloquea la fila del empleado (o el perfil sin equipo) hasta el commit: dos clientes
    pidiendo la misma hora a la vez no pueden quedar los dos con ella."""
    if not all_staff(db, tenant_id):
        db.query(TenantProfile).filter(TenantProfile.tenant_id == tenant_id).with_for_update().first()
        if _has_conflict(db, tenant_id=tenant_id, starts_at=starts_at, ends_at=ends_at, staff_id=None):
            raise ValueError("Ese horario ya está ocupado")
        return None

    if staff_id is not None:
        staff = _lock_staff(db, tenant_id, staff_id)
        if _has_conflict(db, tenant_id=tenant_id, starts_at=starts_at, ends_at=ends_at, staff_id=staff.id):
            raise ValueError(f"{staff.name} ya tiene una cita a esa hora")
        return staff.id

    candidates = free_staff_for_slot(db, tenant_id=tenant_id, starts_at=starts_at, ends_at=ends_at)
    for candidate in candidates:
        _lock_staff(db, tenant_id, candidate.id)
        if not _has_conflict(
            db, tenant_id=tenant_id, starts_at=starts_at, ends_at=ends_at, staff_id=candidate.id
        ):
            return candidate.id
    raise ValueError("No hay nadie libre a esa hora")


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
    staff_id: Optional[uuid.UUID] = None,
    source: str = "panel",
) -> Appointment:
    """Con equipo y sin staff_id, la cita va a quien esté libre (el de menos citas ese día)."""
    if ends_at <= starts_at:
        raise ValueError("La hora de fin debe ser después del inicio")
    if not client_name.strip():
        raise ValueError("Indica el nombre del cliente")

    assigned = _reserve_staff(
        db, tenant_id=tenant_id, starts_at=starts_at, ends_at=ends_at, staff_id=staff_id
    )
    row = Appointment(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        staff_id=assigned,
        starts_at=starts_at.astimezone(timezone.utc),
        ends_at=ends_at.astimezone(timezone.utc),
        client_name=client_name.strip()[:255],
        client_phone=(client_phone or "").strip()[:64] or None,
        notes=(notes or "").strip() or None,
        source=source,
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    return row


_UNCHANGED: Any = object()


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
    staff_id: Any = _UNCHANGED,
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

    new_staff = row.staff_id if staff_id is _UNCHANGED else staff_id
    if all_staff(db, tenant_id):
        if new_staff is None:
            raise ValueError("Elige quién atiende la cita")
        staff = _lock_staff(db, tenant_id, new_staff)
        if _has_conflict(
            db, tenant_id=tenant_id, starts_at=new_start, ends_at=new_end, staff_id=staff.id, exclude_id=row.id
        ):
            raise ValueError(f"{staff.name} ya tiene una cita a esa hora")
    else:
        new_staff = None
        if _has_conflict(
            db, tenant_id=tenant_id, starts_at=new_start, ends_at=new_end, staff_id=None, exclude_id=row.id
        ):
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
    row.staff_id = new_staff
    db.flush()
    db.refresh(row)
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


def upcoming_appointment_for_conversation(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    now: Optional[datetime] = None,
) -> Optional[Appointment]:
    current = now or datetime.now(timezone.utc)
    return (
        db.query(Appointment)
        .filter(
            Appointment.tenant_id == tenant_id,
            Appointment.conversation_id == conversation_id,
            Appointment.ends_at > current,
        )
        .order_by(Appointment.starts_at.asc())
        .first()
    )


def upcoming_appointments_for_conversation(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    now: Optional[datetime] = None,
) -> list[Appointment]:
    """Un cliente puede tener varias: la suya y la de su esposa a la misma hora."""
    current = now or datetime.now(timezone.utc)
    return (
        db.query(Appointment)
        .filter(
            Appointment.tenant_id == tenant_id,
            Appointment.conversation_id == conversation_id,
            Appointment.ends_at > current,
        )
        .order_by(Appointment.starts_at.asc())
        .limit(5)
        .all()
    )


def _same_client(client_phone: str, contact_phone: str, contact_jid: str) -> bool:
    from app.shared.core.phone import is_lid_placeholder, lid_digits_from_jid, phone_match_tail

    if is_lid_placeholder(client_phone):
        lid = lid_digits_from_jid(contact_jid)
        return client_phone == contact_phone or bool(lid and client_phone == f"lid:{lid}")
    return not is_lid_placeholder(contact_phone) and phone_match_tail(client_phone, contact_phone)


def relink_orphan_appointments(db: Session, *, conversation, now: Optional[datetime] = None) -> int:
    """Al desvincular WhatsApp o fusionar chats duplicados se borra el chat, pero la cita queda
    (sin chat). Cuando el mismo cliente vuelve a aparecer, sus citas próximas regresan a su chat."""
    if not (conversation.contact_phone or conversation.contact_jid):
        return 0
    current = now or datetime.now(timezone.utc)
    orphans = (
        db.query(Appointment)
        .filter(
            Appointment.tenant_id == conversation.tenant_id,
            Appointment.conversation_id.is_(None),
            Appointment.client_phone.isnot(None),
            Appointment.ends_at > current,
        )
        .all()
    )
    linked = 0
    for row in orphans:
        if _same_client(row.client_phone or "", conversation.contact_phone or "", conversation.contact_jid or ""):
            row.conversation_id = conversation.id
            linked += 1
    return linked


def describe_appointment(row: Appointment, *, today: Optional[date] = None, with_staff: bool = True) -> str:
    local = row.starts_at.astimezone(BOGOTA)
    ref = today or datetime.now(BOGOTA).date()
    text = f"{_human_day_label(local.date(), ref)} {local.date().isoformat()} a las {local.strftime('%H:%M')}"
    if with_staff and row.staff is not None:
        text += f" con {row.staff.name}"
    return text


def _human_day_label(day: date, today: date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "Hoy"
    if delta == 1:
        return "Mañana"
    names = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
    return names[day.weekday()]

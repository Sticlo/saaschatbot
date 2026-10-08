from __future__ import annotations

import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.application.appointments.appointment_service import _parse_hhmm, _format_hhmm, all_staff
from app.application.conversations.interest_alert_service import InterestAlertError, normalize_alert_phone
from app.domain.entities import Appointment, StaffMember

MAX_STAFF = 30


class StaffError(ValueError):
    pass


def _clean_name(raw: str) -> str:
    name = " ".join(str(raw or "").split())[:80]
    if not name:
        raise StaffError("Escribe el nombre")
    return name


def _clean_phone(raw: Optional[str]) -> Optional[str]:
    try:
        return normalize_alert_phone(raw or "") or None
    except InterestAlertError as exc:
        raise StaffError(str(exc)) from exc


def _clean_days(raw: list[Any]) -> list[int]:
    days = sorted({int(d) for d in raw if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6})
    if not days:
        raise StaffError("Elige al menos un día de trabajo")
    return days


def _clean_hours(start: Optional[str], end: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    start_clock = _parse_hhmm(start) if (start or "").strip() else None
    end_clock = _parse_hhmm(end) if (end or "").strip() else None
    if start_clock and end_clock and end_clock <= start_clock:
        raise StaffError("La hora de salida debe ser después de la de entrada")
    return (
        _format_hhmm(start_clock) if start_clock else None,
        _format_hhmm(end_clock) if end_clock else None,
    )


def _get(db: Session, tenant_id: uuid.UUID, staff_id: uuid.UUID) -> StaffMember:
    staff = (
        db.query(StaffMember)
        .filter(StaffMember.id == staff_id, StaffMember.tenant_id == tenant_id)
        .first()
    )
    if staff is None:
        raise StaffError("Esa persona no está en tu equipo")
    return staff


def _check_unique_name(db: Session, tenant_id: uuid.UUID, name: str, exclude: Optional[uuid.UUID] = None) -> None:
    key = name_key(name)
    for other in all_staff(db, tenant_id):
        if other.id != exclude and name_key(other.name) == key:
            raise StaffError(f"Ya hay alguien llamado {other.name} en el equipo")


def create_staff(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    name: str,
    phone: Optional[str] = None,
    work_days: list[Any],
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    is_active: bool = True,
) -> StaffMember:
    """El primero del equipo se queda con las citas próximas que ya existían sin asignar."""
    team = all_staff(db, tenant_id)
    if len(team) >= MAX_STAFF:
        raise StaffError(f"Máximo {MAX_STAFF} personas en el equipo")
    clean_name = _clean_name(name)
    _check_unique_name(db, tenant_id, clean_name)
    start, end = _clean_hours(start_time, end_time)
    staff = StaffMember(
        tenant_id=tenant_id,
        name=clean_name,
        phone=_clean_phone(phone),
        work_days=_clean_days(work_days),
        start_time=start,
        end_time=end,
        is_active=is_active,
        position=(max((s.position for s in team), default=-1) + 1),
    )
    db.add(staff)
    db.flush()
    if not team:
        db.query(Appointment).filter(
            Appointment.tenant_id == tenant_id,
            Appointment.staff_id.is_(None),
            Appointment.ends_at > datetime.now(timezone.utc),
        ).update({Appointment.staff_id: staff.id}, synchronize_session=False)
    db.refresh(staff)
    return staff


def update_staff(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    staff_id: uuid.UUID,
    name: str,
    phone: Optional[str] = None,
    work_days: list[Any],
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    is_active: bool = True,
) -> StaffMember:
    staff = _get(db, tenant_id, staff_id)
    clean_name = _clean_name(name)
    _check_unique_name(db, tenant_id, clean_name, exclude=staff.id)
    staff.name = clean_name
    staff.phone = _clean_phone(phone)
    staff.work_days = _clean_days(work_days)
    staff.start_time, staff.end_time = _clean_hours(start_time, end_time)
    staff.is_active = is_active
    db.flush()
    db.refresh(staff)
    return staff


def upcoming_count(db: Session, staff_id: uuid.UUID) -> int:
    return (
        db.query(func.count(Appointment.id))
        .filter(Appointment.staff_id == staff_id, Appointment.ends_at > datetime.now(timezone.utc))
        .scalar()
        or 0
    )


def delete_staff(db: Session, *, tenant_id: uuid.UUID, staff_id: uuid.UUID) -> None:
    staff = _get(db, tenant_id, staff_id)
    pending = upcoming_count(db, staff.id)
    if pending:
        raise StaffError(
            f"{staff.name} tiene {pending} cita{'s' if pending != 1 else ''} próxima{'s' if pending != 1 else ''}. "
            "Pásalas a otra persona o márcalo como no disponible."
        )
    db.delete(staff)
    db.flush()


def name_key(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value or "")
    return " ".join("".join(c for c in plain if not unicodedata.combining(c)).lower().split())


def find_staff_by_name(staff: list[StaffMember], raw: str) -> Optional[StaffMember]:
    """'mateo', 'Mateo López' o 'MATEO' encuentran a Mateo; si es ambiguo no adivina."""
    key = name_key(raw)
    if not key:
        return None
    exact = [s for s in staff if name_key(s.name) == key]
    if exact:
        return exact[0]
    partial = [
        s
        for s in staff
        if name_key(s.name).split()[0] == key.split()[0] or key in name_key(s.name)
    ]
    return partial[0] if len(partial) == 1 else None

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.application.appointments.appointment_service import (
    create_appointment,
    parse_slot_to_datetimes,
)
from app.domain.entities import Appointment, Conversation, Tenant

log = logging.getLogger(__name__)

_DAY_NAMES = {
    "lunes": 0,
    "martes": 1,
    "miércoles": 2,
    "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sábado": 5,
    "sabado": 5,
    "domingo": 6,
}

_SUBSTRING_HINTS = (
    "horario",
    "disponib",
    "reserv",
    "agendar",
    "cuando pueden",
    "cuándo pueden",
    "a qué hora",
    "que hora",
    "qué hora",
)

_WORD_HINTS = (
    "cita",
    "turno",
    "mesa",
    "habitacion",
    "habitación",
)

_CONFIRM_HINTS = (
    "confirmo",
    "confirmar",
    "ese horario",
    "esa hora",
    "me sirve",
    "me queda bien",
    "dale",
    "listo",
    "perfecto",
    "sí ",
    "si ",
    "ok",
    "de una",
)

_TIME_RE = re.compile(
    r"(?:a las?|las?|para las?)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.?\s*m\.?|p\.?\s*m\.?)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BookSlotRequest:
    date: str
    start: str
    end: str
    notes: str = ""


@dataclass(frozen=True)
class BookingContext:
    """Agenda que ve la IA cuando el negocio la deja agendar."""

    free_slots: list[dict[str, Any]]
    existing_appointment: str = ""


def slot_key(date_iso: str, start: str, end: str) -> tuple[str, str, str]:
    return date_iso.strip(), start.strip(), end.strip()


def detect_scheduling_interest(text: str) -> bool:
    lower = (text or "").lower()
    if any(h in lower for h in _SUBSTRING_HINTS):
        return True
    return any(re.search(rf"\b{re.escape(word)}\b", lower) for word in _WORD_HINTS)


def detect_slot_confirmation(text: str) -> bool:
    lower = (text or "").lower().strip()
    if not lower:
        return False
    if any(h in lower for h in _CONFIRM_HINTS):
        return True
    return bool(_TIME_RE.search(lower))


def format_slot_line(slot: dict[str, Any]) -> str:
    label = slot.get("label") or f"{slot['date']} {slot['start']}–{slot['end']}"
    return f'- {label} | date="{slot["date"]}" start="{slot["start"]}" end="{slot["end"]}"'


_BOOKING_RULES = """
Agenda de citas: tú puedes agendar, mirando estos horarios libres (no hay otros):
{slot_lines}
{existing}
- Si preguntan por cita, reserva u horario, ofrece 2 a 4 opciones cercanas a lo que piden.
- Agenda solo cuando el cliente elija un horario concreto de la lista: pon book_slot
  {{"date":"YYYY-MM-DD","start":"HH:MM","notes":"para qué es y su nombre si lo dio"}}
  y en message confirma el día y la hora.
- Si pide un horario que no está en la lista, dile que ese no está disponible y ofrece los más cercanos.
- Si solo informas opciones o el cliente no ha elegido, book_slot debe ser null.
- Pedir o agendar una cita no es "cierre": tú la agendas, usa stage "interesado".
"""


def _group_slots_by_day(free_slots: list[dict[str, Any]]) -> list[str]:
    days: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for slot in free_slots:
        day = str(slot["date"])
        days.setdefault(day, []).append(str(slot["start"]))
        labels.setdefault(day, str(slot.get("label") or day).split(" ")[0])
    return [f"- {labels[day]} ({day}): {', '.join(starts)}" for day, starts in days.items()]


def append_appointment_instructions(
    base_prompt: str,
    free_slots: list[dict[str, Any]],
    *,
    existing_appointment: str = "",
) -> str:
    existing = (
        f"- Este cliente ya tiene cita: {existing_appointment}. No agendes otra salvo que pida "
        "una cita adicional; si quiere cambiarla o cancelarla, dile que ya le confirmas (stage cierre).\n"
        if existing_appointment
        else ""
    )
    if not free_slots:
        return base_prompt.rstrip() + (
            "\n\nAgenda de citas: no quedan horarios libres en los próximos días. "
            "Si piden cita, dile que ya le confirmas disponibilidad (stage cierre).\n" + existing
        )
    duration = _slot_minutes(free_slots[0])
    slot_lines = "\n".join(_group_slots_by_day(free_slots))
    if duration:
        slot_lines = f"(cada cita dura {duration} min)\n{slot_lines}"
    block = _BOOKING_RULES.format(slot_lines=slot_lines, existing=existing)
    return base_prompt.rstrip() + "\n" + block


def _slot_minutes(slot: dict[str, Any]) -> Optional[int]:
    try:
        sh, sm = (int(x) for x in str(slot["start"]).split(":"))
        eh, em = (int(x) for x in str(slot["end"]).split(":"))
    except (KeyError, ValueError):
        return None
    minutes = (eh * 60 + em) - (sh * 60 + sm)
    return minutes if minutes > 0 else None


def slot_taken_message(free_slots: list[dict[str, Any]]) -> str:
    options = free_slots[:3]
    if not options:
        return "Uy, ese horario se acaba de ocupar 🙈 Dame un momentico y te confirmo otro."
    lines = ["Uy, ese horario se acaba de ocupar 🙈 Tengo libre:"]
    lines += [f"• {slot.get('label') or slot['date'] + ' ' + slot['start']}" for slot in options]
    lines.append("¿Cuál te sirve?")
    return "\n".join(lines)


def valid_book_slot_keys(free_slots: list[dict[str, Any]]) -> frozenset[tuple[str, str, str]]:
    return frozenset(slot_key(s["date"], s["start"], s["end"]) for s in free_slots)


def parse_book_slot(raw: Any, *, valid_keys: frozenset[tuple[str, str, str]]) -> Optional[BookSlotRequest]:
    if not isinstance(raw, dict):
        return None
    date_iso = str(raw.get("date") or "").strip()
    start = str(raw.get("start") or "").strip()
    end = str(raw.get("end") or "").strip()
    if not date_iso or not start:
        return None
    key = slot_key(date_iso, start, end)
    if key not in valid_keys:
        # La IA suele omitir la hora de fin: basta con que día e inicio sean un bloque libre.
        matches = sorted(k for k in valid_keys if k[0] == date_iso and k[1] == start)
        if not matches:
            return None
        key = matches[0]
    notes = str(raw.get("notes") or "").strip()[:500]
    return BookSlotRequest(date=key[0], start=key[1], end=key[2], notes=notes)


def _resolve_day_from_text(text: str, today: date) -> Optional[date]:
    lower = (text or "").lower()
    if "pasado mañana" in lower or "pasado manana" in lower:
        return today + timedelta(days=2)
    if "mañana" in lower or "manana" in lower:
        return today + timedelta(days=1)
    if "hoy" in lower:
        return today
    for name, weekday in _DAY_NAMES.items():
        if name in lower:
            delta = (weekday - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + timedelta(days=delta)
    return None


def _parse_clock(text: str) -> Optional[tuple[int, int]]:
    match = _TIME_RE.search(text or "")
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower().replace(".", "").replace(" ", "")
    if meridiem.startswith("p") and hour < 12:
        hour += 12
    elif meridiem.startswith("a") and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def match_slot_from_message(
    text: str,
    free_slots: list[dict[str, Any]],
    *,
    today: Optional[date] = None,
) -> Optional[dict[str, Any]]:
    if not free_slots:
        return None
    ref = today or date.today()
    target_day = _resolve_day_from_text(text, ref)
    clock = _parse_clock(text)

    candidates = free_slots
    if target_day:
        day_iso = target_day.isoformat()
        candidates = [s for s in free_slots if s.get("date") == day_iso]
        if not candidates:
            return None

    if clock:
        hour, minute = clock
        target = f"{hour:02d}:{minute:02d}"
        for slot in candidates:
            if slot.get("start") == target:
                return slot
        for slot in candidates:
            start = str(slot.get("start") or "")
            if start.startswith(f"{hour:02d}:"):
                return slot

    if detect_slot_confirmation(text) and len(candidates) == 1:
        return candidates[0]
    return None


def booking_proposal_message(
    *,
    business_name: str,
    contact_name: str,
    free_slots: list[dict[str, Any]],
) -> str:
    name = (contact_name or "").strip()
    greeting = f"¡Perfecto{', ' + name if name else ''}!"
    slots = free_slots[:4]
    if not slots:
        return (
            f"{greeting} Queremos agendarte — en un momentico alguien de "
            f"{business_name or 'el equipo'} te confirma el horario disponible."
        )
    lines = [f"{greeting} Tengo estos horarios libres:"]
    for slot in slots:
        lines.append(f"• {slot.get('label', slot['start'])}")
    lines.append("¿Cuál te queda bien? Dime el horario y te lo separo.")
    return "\n".join(lines)


def booking_confirmation_message(
    *,
    business_name: str,
    slot: dict[str, Any],
    client_name: str,
) -> str:
    name = (client_name or "tu").strip()
    label = slot.get("label") or f"{slot['date']} {slot['start']}"
    biz = (business_name or "nosotros").strip()
    return (
        f"¡Listo, {name}! ✅ Te agendé para {label}. "
        f"Te esperamos en {biz}. Si necesitas cambiar, avísanos por aquí."
    )


def try_create_booking(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    slot: BookSlotRequest | dict[str, Any],
    client_name: str,
    client_phone: Optional[str] = None,
    message_notes: str = "",
) -> Optional[Appointment]:
    if isinstance(slot, BookSlotRequest):
        date_iso, start, end, notes = slot.date, slot.start, slot.end, slot.notes
    else:
        date_iso = str(slot["date"])
        start = str(slot["start"])
        end = str(slot["end"])
        notes = str(slot.get("notes") or "")

    try:
        day = date.fromisoformat(date_iso)
        starts_at, ends_at = parse_slot_to_datetimes(day, start, end)
        merged_notes = " | ".join(
            p for p in (notes, message_notes) if p and p.strip()
        ).strip() or None
        return create_appointment(
            db,
            tenant_id=tenant.id,
            starts_at=starts_at,
            ends_at=ends_at,
            client_name=client_name or conversation.contact_name or "Cliente",
            client_phone=client_phone or conversation.contact_phone,
            notes=merged_notes,
            conversation_id=conversation.id,
        )
    except (ValueError, TypeError) as exc:
        log.warning("No se pudo crear cita IA tenant=%s: %s", tenant.id, exc)
        return None

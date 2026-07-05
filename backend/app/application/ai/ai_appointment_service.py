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
from app.domain.entities import Conversation, Tenant

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


def append_appointment_instructions(base_prompt: str, free_slots: list[dict[str, Any]]) -> str:
    if not free_slots:
        return base_prompt.rstrip() + (
            "\n\nNo hay horarios libres cargados en la agenda. "
            "Si piden cita, di que el equipo confirma disponibilidad pronto."
        )
    lines = [format_slot_line(s) for s in free_slots]
    block = """
Horarios libres en la agenda (solo puedes ofrecer estos — no inventes otros):
{slot_lines}

Si preguntan por cita u horario, ofrece 2 a 4 opciones de la lista.
Si el cliente elige uno de la lista, confirma la reserva con book_slot en el JSON.

Responde SOLO con JSON válido (sin markdown):
{{"message":"tu respuesta","shortcut_id":null,"book_slot":null}}
o al confirmar horario de la lista:
{{"message":"confirmación breve","shortcut_id":null,"book_slot":{{"date":"YYYY-MM-DD","start":"HH:MM","end":"HH:MM","notes":"qué pidió"}}}}

Reglas:
- book_slot solo con date/start/end de la lista de horarios libres.
- Si solo informas opciones, book_slot debe ser null.
""".format(slot_lines="\n".join(lines))
    return base_prompt.rstrip() + "\n" + block


def valid_book_slot_keys(free_slots: list[dict[str, Any]]) -> frozenset[tuple[str, str, str]]:
    return frozenset(slot_key(s["date"], s["start"], s["end"]) for s in free_slots)


def parse_book_slot(raw: Any, *, valid_keys: frozenset[tuple[str, str, str]]) -> Optional[BookSlotRequest]:
    if not isinstance(raw, dict):
        return None
    date_iso = str(raw.get("date") or "").strip()
    start = str(raw.get("start") or "").strip()
    end = str(raw.get("end") or "").strip()
    if not date_iso or not start or not end:
        return None
    key = slot_key(date_iso, start, end)
    if key not in valid_keys:
        return None
    notes = str(raw.get("notes") or "").strip()
    return BookSlotRequest(date=date_iso, start=start, end=end, notes=notes)


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
) -> bool:
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
        create_appointment(
            db,
            tenant_id=tenant.id,
            starts_at=starts_at,
            ends_at=ends_at,
            client_name=client_name or conversation.contact_name or "Cliente",
            client_phone=client_phone or conversation.contact_phone,
            notes=merged_notes,
            conversation_id=conversation.id,
        )
        return True
    except (ValueError, TypeError) as exc:
        log.warning("No se pudo crear cita IA tenant=%s: %s", tenant.id, exc)
        return False

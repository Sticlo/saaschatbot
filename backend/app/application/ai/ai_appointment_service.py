from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.application.appointments.appointment_service import (
    DEFAULT_STAFF_LABEL,
    active_staff,
    create_appointment,
    free_staff_for_slot,
    parse_slot_to_datetimes,
)
from app.application.appointments.staff_service import find_staff_by_name, name_key
from app.domain.entities import Appointment, Conversation, StaffMember, Tenant

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
    staff: str = ""  # nombre que pidió el cliente; vacío = quien esté libre


@dataclass(frozen=True)
class BookingContext:
    """Agenda que ve la IA cuando el negocio la deja agendar."""

    free_slots: list[dict[str, Any]]
    existing_appointment: str = ""
    staff_label: str = ""
    covered_until: str = ""  # YYYY-MM-DD: la lista trae todos los días hasta esta fecha
    extra_days: tuple[str, ...] = ()  # fechas pedidas por el cliente más allá de covered_until
    booked: tuple[tuple[str, str, str], ...] = ()  # citas de este chat: (YYYY-MM-DD, HH:MM, quién atiende)


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
Agenda de citas: tú puedes agendar, mirando estos horarios libres:
{slot_lines}
{coverage}
{existing}
- Solo puedes ofrecer días y horas que aparezcan en esta lista. Nunca inventes otros.
- Si preguntan por cita, reserva u horario, ofrece 2 a 4 opciones cercanas a lo que piden.
- Agenda solo cuando el cliente elija un horario concreto de la lista: pon book_slot
  {{"date":"YYYY-MM-DD","start":"HH:MM","notes":"para qué es y su nombre si lo dio"}}
  y en message dile que quedó agendada (día y hora). No le pidas que la confirme: ya quedó en la agenda.
- Si pide un día u hora cubierto por la lista que no aparece, ese ya no está disponible: ofrece los más cercanos.
- Si solo informas opciones o el cliente no ha elegido, book_slot debe ser null.
- Esta lista es la agenda real y completa, y nadie más la va a revisar: nunca digas "déjame revisar",
  "ya te confirmo" ni "te confirmo en un momentico". Responde de una con lo que dice la lista.
- "Epa", "breve", "de una", "hágale", "sisas", "melo", "dale", "va", "listo", "ok" y parecidos solo significan
  "sí / perfecto". Si ya agendaste, no vuelvas a poner book_slot: responde corto y amable.
- Pedir o agendar una cita no es "cierre": tú la agendas, usa stage "interesado".
"""


def _coverage_note(covered_until: str, extra_days: tuple[str, ...]) -> str:
    if not covered_until:
        return ""
    extra = f" y además el {', '.join(extra_days)}" if extra_days else ""
    return (
        f"(La lista trae todos los días hasta el {covered_until}{extra}. Un día de ese rango que no aparece "
        "está lleno o no se atiende. Para otra fecha posterior NO digas que está llena: no la estás viendo; "
        "pídele que te confirme la fecha exacta, día y mes, y se la revisas.)"
    )


_TEAM_RULES = """
Equipo ({plural}): {names}.
- Una hora sola ("15:00") = todo el equipo está libre. Una hora con "(libres: X, Y)" = a esa hora solo X e Y
  están libres; los demás ya tienen cita. Esta lista manda sobre lo que se haya dicho antes en el chat.
- Si el cliente pide a alguien del equipo, ofrece solo las horas de esa persona.
- Que alguien esté ocupado a una hora no bloquea a los demás: si a esa hora aparecen otros libres, ofrécelos.
  Si el cliente se llama igual que alguien del equipo, no los confundas.
- Si no ha dicho con quién, pregunta una sola vez: "¿Tienes {label} de preferencia o te agendo con quien esté libre?".
  Si le da igual o solo te dice la hora, agéndalo con quien esté libre.
- En book_slot agrega "staff": el nombre que pidió, o null si le da igual. Si le da igual no prometas a nadie:
  el sistema elige y le dice con quién quedó.
"""


def team_names(free_slots: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for slot in free_slots:
        for member in slot.get("staff") or []:
            if member["name"] not in names:
                names.append(member["name"])
    return names


def _group_slots_by_day(free_slots: list[dict[str, Any]]) -> list[str]:
    everyone = set(team_names(free_slots))
    days: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for slot in free_slots:
        day = str(slot["date"])
        entry = str(slot["start"])
        free = [m["name"] for m in slot.get("staff") or []]
        if free and set(free) != everyone:
            entry += f" (libres: {', '.join(free)})"
        days.setdefault(day, []).append(entry)
        labels.setdefault(day, str(slot.get("label") or day).split(" ")[0])
    return [f"- {labels[day]} ({day}): {', '.join(starts)}" for day, starts in days.items()]


def append_appointment_instructions(
    base_prompt: str,
    free_slots: list[dict[str, Any]],
    *,
    existing_appointment: str = "",
    staff_label: str = "",
    covered_until: str = "",
    extra_days: tuple[str, ...] = (),
) -> str:
    existing = (
        f"- Este cliente ya tiene cita: {existing_appointment}. Ya están agendadas: no las repitas en book_slot. "
        "Esa hora ya no aparece libre para quien lo atiende. "
        "Si pide otra cita (para él o para otra persona, como su esposa o su hijo), agéndala como una cita nueva "
        "con book_slot y en notes pon para quién es. Si quiere cambiar o cancelar la suya, dile que le pasas el "
        "cambio al equipo (stage cierre).\n"
        if existing_appointment
        else ""
    )
    if not free_slots:
        until = f" hasta el {covered_until}" if covered_until else " en los próximos días"
        return base_prompt.rstrip() + (
            f"\n\nAgenda de citas: no quedan horarios libres{until}. "
            "Si piden cita, dile que le pasas la solicitud al equipo para que le confirmen (stage cierre).\n"
            + existing
        )
    duration = _slot_minutes(free_slots[0])
    slot_lines = "\n".join(_group_slots_by_day(free_slots))
    if duration:
        slot_lines = f"(cada cita dura {duration} min)\n{slot_lines}"
    block = _BOOKING_RULES.format(
        slot_lines=slot_lines, coverage=_coverage_note(covered_until, extra_days), existing=existing
    )
    names = team_names(free_slots)
    if names:
        label = staff_label or DEFAULT_STAFF_LABEL
        plural = label + ("s" if label[-1] in "aeiouáéó" else "es")
        block += _TEAM_RULES.format(label=label, plural=plural, names=", ".join(names))
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
    staff = raw.get("staff")
    staff = str(staff).strip()[:80] if isinstance(staff, str) and staff.strip().lower() != "null" else ""
    return BookSlotRequest(date=key[0], start=key[1], end=key[2], notes=notes, staff=staff)


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


_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
_MONTH_ALT = "|".join(_MONTHS)
_DAY_MONTH_RE = re.compile(rf"\b(\d{{1,2}})\s*(?:de\s+)?({_MONTH_ALT})\b")
_MONTH_DAY_RE = re.compile(rf"\b({_MONTH_ALT})\s+(\d{{1,2}})\b")
_NUMERIC_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
# "el 15", "para el 15", "el día 15" — pero no "el 15 a las…" confundido con una hora ("a las 3").
_DAY_ONLY_RE = re.compile(r"\b(?:el|para el|d[ií]a)\s+(?:d[ií]a\s+)?(\d{1,2})\b(?!\s*(?::|am\b|pm\b|a\.\s?m|p\.\s?m|de la))")
REQUESTED_DAYS_MAX_AHEAD = 90


def _next_occurrence(today: date, month: int, day: int) -> Optional[date]:
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate
    return None


def requested_dates(text: str, today: date) -> list[date]:
    """Fechas concretas que pide el cliente ("15 de octubre", "octubre 15", "15/10", "el 15").

    Un día sin año es su próxima vez; las que pasan de 90 días o no existen se ignoran."""
    lower = (text or "").lower()
    found: set[date] = set()
    for day_s, month_s in _DAY_MONTH_RE.findall(lower):
        found.add(_next_occurrence(today, _MONTHS[month_s], int(day_s)))
    for month_s, day_s in _MONTH_DAY_RE.findall(lower):
        found.add(_next_occurrence(today, _MONTHS[month_s], int(day_s)))
    for day_s, month_s, _year in _NUMERIC_RE.findall(lower):
        if 1 <= int(month_s) <= 12:
            found.add(_next_occurrence(today, int(month_s), int(day_s)))
    if not found:
        for day_s in _DAY_ONLY_RE.findall(lower):
            day = int(day_s)
            month, year = today.month, today.year
            if day < today.day:
                month, year = (1, year + 1) if month == 12 else (month + 1, year)
            try:
                found.add(date(year, month, day))
            except ValueError:
                pass
    limit = today + timedelta(days=REQUESTED_DAYS_MAX_AHEAD)
    return sorted(d for d in found if d is not None and today <= d <= limit)


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


_CLAIM_RE = re.compile(
    r"\b(?:te\s+(?:dejo|deje|tengo|queda|quedo)\s+(?:agendad|reservad|apartad|anotad)"
    r"|ya\s+te\s+(?:agende|reserve|aparte|anote)\b"
    r"|(?:quedo|queda|quedaste|quedas)\s+(?:agendad|reservad|apartad|confirmad)"
    r"|ya\s+(?:esta|quedo)\s+(?:agendad|reservad|apartad|confirmad|list)"
    r"|tu\s+(?:cita|turno|reserva)\s+(?:quedo|queda|esta)\b"
    r"|(?:cita|turno|reserva)\s+(?:agendad|confirmad|reservad))"
)
# Sin tilde, «que te agende» (pregunta) y «te agendé» (hecho) se escriben igual.
_PAST_CLAIM_RE = re.compile(r"\bte\s+(?:agendé|reservé|aparté|anoté)\b")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!…])\s+|\n+")
_CONDITIONAL_RE = re.compile(r"^(?:si|cuando|apenas|en cuanto)\b|\bsi\s+(?:me|quieres|gustas|deseas|prefieres)\b")
_CLOCK_HHMM_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(a\.?\s*m\.?|p\.?\s*m\.?)?")
_CLOCK_LAS_RE = re.compile(r"\blas?\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?\s*m\.?|p\.?\s*m\.?|de la manana|de la tarde)?")


def claims_booking(message: str) -> bool:
    """«Te dejo agendado», «te agendé», «tu cita quedó…»: la IA le dice al cliente que ya tiene cita.
    Preguntas y ofertas («¿quieres que te agende?», «si me dices la hora te dejo agendado») no cuentan."""
    for sentence in _SENTENCE_SPLIT_RE.split(message or ""):
        if "?" in sentence or "¿" in sentence:
            continue
        plain = name_key(sentence)
        if _CONDITIONAL_RE.search(plain):
            continue
        if _CLAIM_RE.search(plain) or _PAST_CLAIM_RE.search(sentence.lower()):
            return True
    return False


def _clock_in_confirmation(text: str) -> Optional[str]:
    match = _CLOCK_HHMM_RE.search(text) or _CLOCK_LAS_RE.search(text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "").replace(" ", "")
    if (meridiem.startswith("p") or "tarde" in meridiem) and hour < 12:
        hour += 12
    elif meridiem.startswith("a") and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def infer_claimed_booking(
    message: str, free_slots: list[dict[str, Any]], *, today: Optional[date] = None
) -> Optional[BookSlotRequest]:
    """La IA a veces confirma la cita en el texto y olvida book_slot. Su propio mensaje dice
    qué agendó («miércoles 21 de octubre a las 10:00 con Juan»): si ese bloque está libre, lo usamos."""
    if not claims_booking(message):
        return None
    text = name_key(message)
    ref = today or date.today()
    days = requested_dates(text, ref)
    if not days:
        relative = _resolve_day_from_text(text, ref)
        days = [relative] if relative else []
    clock = _clock_in_confirmation(text)
    if len(days) != 1 or not clock:
        return None
    slot = next((s for s in free_slots if s["date"] == days[0].isoformat() and s["start"] == clock), None)
    if slot is None:
        return None
    team = slot.get("staff") or []
    words = set(re.findall(r"\w+", text))
    named = [m["name"] for m in team if name_key(m["name"]).split()[0] in words]
    if not named:
        everyone = {name_key(n).split()[0] for n in team_names(free_slots)}
        if everyone & words:
            return None  # nombró a alguien del equipo que no está libre a esa hora
    return BookSlotRequest(date=slot["date"], start=slot["start"], end=slot["end"], staff=named[0] if named else "")


_STALL_RE = re.compile(
    r"\b(?:ya\s+te\s+(?:confirmo|cuento|aviso|digo)\b"
    r"|te\s+(?:confirmo|aviso|cuento)\s+(?:en\s+)?(?:un\s+)?(?:momentico|momentito|momento|ratico|rato)\b"
    r"|(?:dejame|permiteme|deja\s+y)\s+(?:revis|verific|consult|mir|cheque|confirm)"
    r"|dame\s+un\s+(?:momentico|momentito|momento|segundo|ratico)\s+y\s+te\s+(?:confirmo|aviso|cuento|digo)"
    r"|voy\s+a\s+(?:revisar|verificar|consultar|mirar|chequear)"
    r"|ya\s+(?:lo\s+|te\s+)?(?:reviso|verifico|consulto|chequeo)\b"
    r"|(?:en\s+cuanto|apenas|cuando|tan\s+pronto)\s+(?:tenga|sepa|me\s+confirmen|confirme|lo\s+confirme|revise)"
    r"|te\s+(?:escribo|aviso|confirmo|cuento)\s+(?:por\s+aqui\s+)?(?:en\s+cuanto|apenas|cuando|tan\s+pronto)"
    r"|(?:quedo|quedamos)\s+(?:atent[oa]|pendientes?)\s+(?:a|de)\s+(?:la\s+)?confirm)"
)
_AGENDA_RE = re.compile(
    r"\b(?:cita|agend|horario|hora\b|disponib|profesional|turno|reserv|libre)|\d{1,2}:\d{2}|\blas\s+\d{1,2}\b"
)


def promises_follow_up(message: str) -> bool:
    """«Ya te confirmo», «déjame revisar»: quien lo lee espera otro mensaje."""
    return bool(_STALL_RE.search(name_key(message)))


def stalls_on_agenda(message: str, client_text: str, free_slots: list[dict[str, Any]]) -> bool:
    """«Déjame revisar si hay otro profesional y te confirmo»: con la agenda delante, eso deja al
    cliente esperando una respuesta que nadie va a dar. Un «ya te confirmo» al cerrar una venta sí vale."""
    reply = name_key(message)
    if not _STALL_RE.search(reply):
        return False
    context = f"{reply} {name_key(client_text)}"
    if _AGENDA_RE.search(context):
        return True
    words = set(re.findall(r"\w+", context))
    return any(name_key(n).split()[0] in words for n in team_names(free_slots))


def _requested_starts(text: str) -> set[str]:
    """«a las 11 am» → {"11:00"}; sin am/pm, «a las 2» puede ser 02:00 o 14:00."""
    key = name_key(text)
    match = _CLOCK_HHMM_RE.search(key) or _CLOCK_LAS_RE.search(key)
    if not match:
        return set()
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "").replace(" ", "")
    if (meridiem.startswith("p") or "tarde" in meridiem) and hour < 12:
        hours = [hour + 12]
    elif meridiem:
        hours = [hour]
    else:
        hours = [hour, hour + 12] if hour < 12 else [hour]
    return {f"{h:02d}:{minute:02d}" for h in hours}


def requested_slot_note(
    client_text: str,
    free_slots: list[dict[str, Any]],
    day: Optional[str],
    booked: tuple[tuple[str, str, str], ...] = (),
) -> str:
    """El cliente pidió una hora concreta («para las 2»): le decimos a la IA quién está libre,
    en vez de confiar en que lo deduzca de la lista y del chat."""
    if not day:
        return ""
    starts = _requested_starts(client_text)
    if not starts:
        return ""
    day_slots = [s for s in free_slots if s["date"] == day]
    hits = [s for s in day_slots if s["start"] in starts]
    prefix = "Dato verificado ahora en la agenda (manda sobre lo que se haya dicho antes en el chat):"
    own = [(start, who) for d, start, who in booked if d == day and start in starts]
    if own:
        start, who = own[0]
        with_whom = f" con {who}" if who else ""
        others = [m["name"] for m in (hits[0].get("staff") or [])] if hits else []
        extra = (
            f" Si pide otra cita a esa misma hora (para otra persona), están libres {', '.join(others)}."
            if others
            else ""
        )
        return (
            f"{prefix} el {day} a las {start} este cliente YA tiene su cita agendada{with_whom}: es la suya, "
            f"por eso {who or 'esa hora'} no aparece libre. Si confirma, agradece o "
            "pregunta, dile que su cita quedó agendada. No digas que no hay disponibilidad ni la vuelvas a "
            f"agendar.{extra}"
        )
    if not day_slots:
        return ""
    if not hits:
        asked = " o ".join(sorted(starts))
        return f"{prefix} el {day} a las {asked} no hay nadie libre."
    slot = hits[0]
    names = [m["name"] for m in slot.get("staff") or []]
    if not names:
        return f"{prefix} el {day} a las {slot['start']} está libre."
    return (
        f"{prefix} el {day} a las {slot['start']} están libres {', '.join(names)} (del equipo). "
        "Puedes agendar a esa hora con cualquiera de ellos."
    )


def pending_promise_note(previous_bot_message: str) -> str:
    """La IA prometió revisar y confirmar: en el turno siguiente tiende a repetir la promesa."""
    if not promises_follow_up(previous_bot_message) or not _AGENDA_RE.search(name_key(previous_bot_message)):
        return ""
    return (
        f"Tu mensaje anterior le prometió al cliente revisar y confirmarle («{previous_bot_message.strip()}»). "
        "Él está esperando esa respuesta: dásela ahora con la agenda de arriba, sin volver a prometer."
    )


def stall_correction(message: str) -> str:
    return (
        f"IMPORTANTE: ibas a responder «{message.strip()}». No puedes decirle que vas a revisar ni que ya le "
        "confirmas: la agenda de arriba es la real y completa, y nadie más va a revisar. Responde ahora: si lo que "
        "pide está libre en la lista, díselo (y si ya eligió, agéndalo con book_slot); si no está, ofrece las "
        "opciones más cercanas de la lista. Solo si quiere cambiar o cancelar una cita que ya tiene, dile que le "
        "pasas el cambio al equipo."
    )


def available_options_message(free_slots: list[dict[str, Any]], *, day: Optional[str] = None) -> str:
    """Último recurso cuando la IA insiste en «ya te confirmo»: decirle al cliente lo que hay."""
    options = [s for s in free_slots if day and s["date"] == day] or free_slots
    options = options[:4]
    if not options:
        return "Para ese día ya no me quedan horarios libres 🙈 ¿Te sirve otro día?"
    lines = ["Te cuento lo que tengo libre 🙌"]
    for slot in options:
        label = slot.get("label") or f"{slot['date']} {slot['start']}"
        names = [m["name"] for m in slot.get("staff") or []]
        lines.append(f"• {label}" + (f" con {' o '.join(names)}" if names else ""))
    lines.append("¿Cuál te sirve?")
    return "\n".join(lines)


BOOKING_HANDOFF_MESSAGE = "¡De una! Le paso tu solicitud al equipo para que te confirmen la hora 🙌"


def confirm_slot_message(free_slots: list[dict[str, Any]]) -> str:
    """Cuando la IA dio la cita por hecha sin un horario válido: pedirlo, no prometer."""
    options = free_slots[:3]
    if not options:
        return "Para dejarte la cita agendada dime qué día y a qué hora te sirve 🙌"
    lines = ["Para dejarte la cita agendada confírmame el horario 🙌 Tengo libre:"]
    lines += [f"• {slot.get('label') or slot['date'] + ' ' + slot['start']}" for slot in options]
    lines.append("¿Cuál te sirve?")
    return "\n".join(lines)


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
        date_iso, start, end, notes, wanted = slot.date, slot.start, slot.end, slot.notes, slot.staff
    else:
        date_iso = str(slot["date"])
        start = str(slot["start"])
        end = str(slot["end"])
        notes = str(slot.get("notes") or "")
        wanted = str(slot.get("staff") or "")

    try:
        day = date.fromisoformat(date_iso)
        starts_at, ends_at = parse_slot_to_datetimes(day, start, end)
        preferred = requested_staff(db, tenant.id, wanted)
        if preferred is not None and not free_staff_for_slot(
            db, tenant_id=tenant.id, starts_at=starts_at, ends_at=ends_at, preferred_staff_id=preferred.id
        ):
            log.info("IA: %s no está libre a esa hora tenant=%s", preferred.name, tenant.id)
            return None
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
            staff_id=preferred.id if preferred else None,
        )
    except (ValueError, TypeError) as exc:
        log.warning("No se pudo crear cita IA tenant=%s: %s", tenant.id, exc)
        return None


_ACK_WORDS = frozenset(
    "ok okey okay vale dale listo epa breve hagale sisas melo bacano va perfecto gracias bueno "
    "de una super excelente genial chevere hecho entendido claro si pues parce parcero mano hermano "
    "chamo mijo mija entonces muchas".split()
)


def is_short_ack(text: str) -> bool:
    """«Epa», «breve», «de una», «hágale», «listo gracias»: el cliente asiente, no pide nada nuevo."""
    words = re.findall(r"\w+", name_key(text))
    return 0 < len(words) <= 4 and all(w in _ACK_WORDS for w in words)


_ANOTHER_PERSON_RE = re.compile(
    r"\botr[ao]s?\s+(?:cita|turno|corte|persona|reserva)"
    r"|\btambien\b|\bdos\s+citas\b|\bnosotros\b|\blos\s+dos\b"
    r"|\b(?:mi|mis|su|sus)\s+(?:espos[ao]s?|hij[ao]s?|novi[ao]|mama|papa|herman[ao]s?|amig[ao]s?|pareja|"
    r"prim[ao]s?|sobrin[ao]s?|suegr[ao]|abuel[ao]|tio|tia|nin[ao]s?|bebe|mujer|marido|senora|companer[ao]|socio)\b"
)


def mentions_another_person(text: str) -> bool:
    """«y para mi esposa», «otra cita», «también»: la cita a esa hora es para alguien más."""
    return bool(_ANOTHER_PERSON_RE.search(name_key(text)))


def already_booked(
    db: Session,
    *,
    tenant_id,
    conversation_id,
    slot: BookSlotRequest,
    client_text: str,
    inferred: bool = False,
    reply_text: str = "",
) -> Optional[Appointment]:
    """La IA vuelve a mandar book_slot de una cita que ya agendó («Epa» → «¡Listo, quedó con Luis!»).
    Con la misma persona es siempre la misma cita. Sin persona es la misma salvo que el cliente hable
    de otra persona («y para mi esposa a la misma hora») o la IA nombre a otro del equipo."""
    try:
        starts_at, _ = parse_slot_to_datetimes(date.fromisoformat(slot.date), slot.start, slot.end)
    except (ValueError, TypeError):
        return None
    rows = (
        db.query(Appointment)
        .filter(
            Appointment.tenant_id == tenant_id,
            Appointment.conversation_id == conversation_id,
            Appointment.starts_at == starts_at,
        )
        .all()
    )
    if not rows:
        return None
    wanted = requested_staff(db, tenant_id, slot.staff)
    if wanted is not None:
        return next((r for r in rows if r.staff_id == wanted.id), None)
    if inferred or is_short_ack(client_text):
        return rows[-1]
    if mentions_another_person(client_text):
        return None
    words = set(re.findall(r"\w+", name_key(reply_text)))
    named = next((r for r in rows if r.staff is not None and name_key(r.staff.name).split()[0] in words), None)
    return named or rows[-1]


_UNAVAILABLE_RE = re.compile(
    r"\bno\s+(?:tiene|tengo|hay|esta|queda)\s+(?:disponib|libre|cupo|espacio)"
    r"|\bno\s+esta\s+(?:disponible|libre)"
    r"|\b(?:esta|estan)\s+ocupad"
    r"|\bse\s+(?:acaba\s+de\s+|me\s+)?ocup"
)
_WEEKDAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTHS_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def denies_own_booking(message: str, booked: tuple[tuple[str, str, str], ...]) -> Optional[tuple[str, str, str]]:
    """«Juan no tiene disponibilidad a las 11»… cuando esa hora de Juan es la cita de este mismo cliente."""
    key = name_key(message)
    if not booked or not _UNAVAILABLE_RE.search(key):
        return None
    words = set(re.findall(r"\w+", key))
    for entry in booked:
        _day, start, who = entry
        hour = int(start[:2])
        hours = {str(hour), str(hour % 12 or 12)}
        mentions_time = bool(words & hours) or start in key
        mentions_staff = bool(who) and name_key(who).split()[0] in words
        if mentions_time and (mentions_staff or not who):
            return entry
    return None


def own_booking_message(entry: tuple[str, str, str]) -> str:
    day_iso, start, who = entry
    day = date.fromisoformat(day_iso)
    label = f"{_WEEKDAYS_ES[day.weekday()]} {day.day} de {_MONTHS_ES[day.month - 1]}"
    with_whom = f" con {who}" if who else ""
    return f"¡Listo! Tu cita ya quedó agendada para el {label} a las {start}{with_whom} ✅"


def nearest_slots(free_slots: list[dict[str, Any]], *, day: str, start: str, n: int = 3) -> list[dict[str, Any]]:
    """Alternativas a un horario que se ocupó: primero ese mismo día y lo más cerca de esa hora."""
    def minutes(hhmm: str) -> int:
        h, m = (int(x) for x in hhmm.split(":"))
        return h * 60 + m

    asked = minutes(start)
    same_day = sorted((s for s in free_slots if s["date"] == day), key=lambda s: abs(minutes(s["start"]) - asked))
    later = [s for s in free_slots if s["date"] > day]
    return (same_day + later)[:n] or free_slots[:n]


def requested_staff(db: Session, tenant_id, wanted: str) -> Optional[StaffMember]:
    """A quién pidió el cliente; None si le da igual o el nombre no es de nadie del equipo."""
    if not (wanted or "").strip():
        return None
    return find_staff_by_name(active_staff(db, tenant_id), wanted)


def assigned_staff_note(message: str, appointment: Appointment) -> str:
    """Si la IA no dijo con quién quedó, se lo agregamos al cliente."""
    staff = appointment.staff
    if staff is None:
        return message
    if name_key(staff.name).split()[0] in name_key(message):
        return message
    return f"{message.rstrip()}\nTe atiende {staff.name}.".lstrip()

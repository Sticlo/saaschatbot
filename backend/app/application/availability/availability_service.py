"""Disponibilidad real: la IA ve qué fechas están ocupadas en el calendario de cada alojamiento
o espacio (iCal de Airbnb, Booking, Google Calendar…) antes de ofrecerlas."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy.orm import Session

from app.application.appointments.appointment_service import BOGOTA
from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.infrastructure.cache.redis_client import get_redis

log = logging.getLogger(__name__)

MAX_CALENDARS = 20
MAX_NAME_CHARS = 60
MAX_URL_CHARS = 1000
MAX_ICS_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 8.0
HORIZON_DAYS = 90
FRESH_SECONDS = 10 * 60
# Si el calendario no responde un rato, mejor la última copia (con su hora) que nada.
STALE_SECONDS = 2 * 24 * 3600

_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


class CalendarError(Exception):
    """Mensaje listo para mostrarle al dueño."""


def normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://") :]
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise CalendarError("Pega el enlace del calendario completo, el que empieza con https:// (o webcal://).")
    if parts.port not in (None, 443):
        raise CalendarError("Ese enlace no es de un calendario público.")
    return url[:MAX_URL_CHARS]


def normalize_calendars(raw: list[Any]) -> list[dict]:
    out: list[dict] = []
    for item in raw[:MAX_CALENDARS]:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())[:MAX_NAME_CHARS]
        url = str(item.get("url") or "").strip()
        if not name or not url:
            continue
        out.append({"id": str(item.get("id") or uuid.uuid4()), "name": name, "url": normalize_url(url)})
    return out


def calendars_of(profile: Any) -> list[dict]:
    raw = getattr(profile, "availability_calendars", None) if profile is not None else None
    return [c for c in raw if isinstance(c, dict) and c.get("name") and c.get("url")] if isinstance(raw, list) else []


def get_calendars(db: Session, tenant_id: uuid.UUID) -> list[dict]:
    return calendars_of(get_or_create_tenant_profile(db, tenant_id))


def save_calendars(db: Session, tenant_id: uuid.UUID, calendars: list[Any]) -> list[dict]:
    profile = get_or_create_tenant_profile(db, tenant_id)
    cleaned = normalize_calendars(calendars)
    profile.availability_calendars = cleaned
    db.flush()
    return cleaned


def _require_public_host(host: str) -> None:
    """Solo servidores públicos: un enlace no puede hacernos leer la red interna."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise CalendarError("No encontré ese calendario. Revisa que el enlace esté completo.") from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise CalendarError("Ese enlace no es de un calendario público.")


def _download(url: str) -> str:
    for _ in range(MAX_REDIRECTS + 1):
        url = normalize_url(url)
        _require_public_host(urlsplit(url).hostname or "")
        try:
            with httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client:
                with client.stream("GET", url, headers={"User-Agent": "Omitel-Calendar/1.0"}) as resp:
                    if resp.is_redirect:
                        url = urljoin(url, resp.headers.get("location", ""))
                        continue
                    if resp.status_code != 200:
                        raise CalendarError(f"El calendario no respondió (código {resp.status_code}). Revisa el enlace.")
                    data = bytearray()
                    for chunk in resp.iter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_ICS_BYTES:
                            raise CalendarError("Ese calendario es demasiado grande.")
                    return data.decode("utf-8", errors="replace")
        except httpx.HTTPError as exc:
            raise CalendarError("No pude abrir el calendario. Intenta de nuevo en un rato.") from exc
    raise CalendarError("El enlace redirige demasiadas veces.")


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _local_day(params: str, value: str) -> tuple[Optional[date], bool]:
    """(fecha local en Colombia, traía hora)."""
    value = value.strip()
    try:
        if "T" not in value:
            return datetime.strptime(value[:8], "%Y%m%d").date(), False
        moment = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None, False
    if value.endswith("Z"):
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        tzid = next((p.split("=", 1)[1] for p in params.split(";")[1:] if p.upper().startswith("TZID=")), "")
        try:
            moment = moment.replace(tzinfo=ZoneInfo(tzid.strip('"'))) if tzid else moment.replace(tzinfo=BOGOTA)
        except (ZoneInfoNotFoundError, ValueError):
            moment = moment.replace(tzinfo=BOGOTA)
    local = moment.astimezone(BOGOTA)
    return local.date(), True


def parse_ics(text: str) -> list[tuple[date, date]]:
    """Rangos ocupados [llegada, salida): cada fecha del rango es un día o una noche ocupada."""
    events: list[dict[str, tuple[str, str]]] = []
    current: Optional[dict[str, tuple[str, str]]] = None
    for line in _unfold(text):
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
        elif upper == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            params, value = line.split(":", 1)
            current.setdefault(params.split(";", 1)[0].upper(), (params, value))
    ranges: list[tuple[date, date]] = []
    for event in events:
        if event.get("STATUS", ("", ""))[1].strip().upper() == "CANCELLED":
            continue
        start, _ = _local_day(*event["DTSTART"]) if "DTSTART" in event else (None, False)
        if start is None:
            continue
        end, _ = _local_day(*event["DTEND"]) if "DTEND" in event else (None, False)
        if end is None or end <= start:
            end = start + timedelta(days=1)
        ranges.append((start, end))
    return ranges


def _cache_key(url: str, kind: str) -> str:
    return f"avail:ical:{kind}:{hashlib.sha1(url.encode()).hexdigest()}"


def busy_ranges(url: str) -> tuple[Optional[list[tuple[date, date]]], Optional[float], str]:
    """(rangos, desde cuándo son, error). Los rangos son None solo si nunca se pudo leer."""
    try:
        client = get_redis()
        cached = client.get(_cache_key(url, "fresh"))
    except Exception:
        client, cached = None, None
    if cached:
        data = json.loads(cached)
        return [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in data["ranges"]], data["at"], ""
    try:
        today = datetime.now(BOGOTA).date()
        ranges = [
            (a, b) for a, b in parse_ics(_download(url))
            if b > today - timedelta(days=1) and a < today + timedelta(days=400)
        ][:2000]
    except CalendarError as exc:
        stale = None
        try:
            stale = client.get(_cache_key(url, "last")) if client else None
        except Exception:
            pass
        if stale:
            data = json.loads(stale)
            return [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in data["ranges"]], data["at"], str(exc)
        return None, None, str(exc)
    payload = json.dumps({"ranges": [[a.isoformat(), b.isoformat()] for a, b in ranges], "at": time.time()})
    if client is not None:
        try:
            client.set(_cache_key(url, "fresh"), payload, ex=FRESH_SECONDS)
            client.set(_cache_key(url, "last"), payload, ex=STALE_SECONDS)
        except Exception:
            log.debug("No se pudo guardar el calendario en caché", exc_info=True)
    return ranges, time.time(), ""


def busy_days(ranges: list[tuple[date, date]], *, start: date, days: int = HORIZON_DAYS) -> list[date]:
    end = start + timedelta(days=days)
    taken: set[date] = set()
    for a, b in ranges:
        day = max(a, start)
        while day < min(b, end):
            taken.add(day)
            day += timedelta(days=1)
    return sorted(taken)


def _fmt(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]}"


def format_days(days: list[date]) -> str:
    groups: list[tuple[date, date]] = []
    for day in days:
        if groups and day - groups[-1][1] == timedelta(days=1):
            groups[-1] = (groups[-1][0], day)
        else:
            groups.append((day, day))
    parts = []
    for a, b in groups:
        if a == b:
            parts.append(_fmt(a))
        elif a.month == b.month:
            parts.append(f"{a.day}–{b.day} {_MONTHS[a.month - 1]}")
        else:
            parts.append(f"{_fmt(a)} – {_fmt(b)}")
    return ", ".join(parts)


def check_calendar(url: str) -> int:
    """Lee el calendario ya mismo (para el panel): cuántos días ocupados tiene en los próximos 90."""
    ranges = parse_ics(_download(normalize_url(url)))
    return len(busy_days(ranges, start=datetime.now(BOGOTA).date()))


def availability_block(profile: Any, *, today: Optional[date] = None) -> str:
    calendars = calendars_of(profile)
    if not calendars:
        return ""
    today = today or datetime.now(BOGOTA).date()
    horizon = today + timedelta(days=HORIZON_DAYS)
    with ThreadPoolExecutor(max_workers=min(8, len(calendars))) as pool:
        results = list(pool.map(lambda cal: busy_ranges(cal["url"]), calendars))
    lines: list[str] = []
    for cal, (ranges, _, error) in zip(calendars, results):
        if ranges is None:
            log.warning("Calendario sin leer «%s»: %s", cal["name"], error)
            lines.append(
                f"- {cal['name']}: no se pudo revisar su calendario. No confirmes fechas de este: "
                "dile al cliente que se las confirmas."
            )
            continue
        taken = format_days(busy_days(ranges, start=today))
        lines.append(f"- {cal['name']}: ocupado {taken}" if taken else f"- {cal['name']}: todo libre")
    return (
        f"\n\nDisponibilidad real (calendario de cada alojamiento o espacio, de hoy al {_fmt(horizon)}):\n"
        + "\n".join(lines)
        + "\n- Cada fecha es un día ocupado; en alojamiento es la noche que empieza ese día (la salida es al "
        "día siguiente). Lo que no aparece en la lista está libre.\n"
        "- Antes de decir que hay cupo, revisa estas fechas exactas. Si el cliente no ha dicho fechas, "
        "pregúntaselas. En una estadía de varias noches, todas tienen que estar libres.\n"
        "- Si está ocupado, dilo con amabilidad y ofrece otro alojamiento o espacio libre esas mismas fechas, "
        "o las fechas libres más cercanas.\n"
        f"- Después del {_fmt(horizon)} no conoces la disponibilidad: di que la confirmas.\n"
        "- No digas que la fecha quedó reservada: se asegura cuando el negocio confirma el pago o el anticipo."
    )

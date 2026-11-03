"""Festivos de Colombia (Ley 51 de 1983: varios se corren al lunes siguiente)."""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Optional

_FIXED = {
    (1, 1): "Año Nuevo",
    (5, 1): "Día del Trabajo",
    (7, 20): "Día de la Independencia",
    (8, 7): "Batalla de Boyacá",
    (12, 8): "Inmaculada Concepción",
    (12, 25): "Navidad",
}
_TO_MONDAY = {
    (1, 6): "Reyes Magos",
    (3, 19): "San José",
    (6, 29): "San Pedro y San Pablo",
    (8, 15): "Asunción de la Virgen",
    (10, 12): "Día de la Raza",
    (11, 1): "Todos los Santos",
    (11, 11): "Independencia de Cartagena",
}
_FROM_EASTER = {-3: "Jueves Santo", -2: "Viernes Santo"}
_FROM_EASTER_TO_MONDAY = {43: "Ascensión del Señor", 64: "Corpus Christi", 71: "Sagrado Corazón"}


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _next_monday(day: date) -> date:
    return day + timedelta(days=(7 - day.weekday()) % 7)


@lru_cache(maxsize=8)
def holidays(year: int) -> dict[date, str]:
    days = {date(year, m, d): name for (m, d), name in _FIXED.items()}
    days.update({_next_monday(date(year, m, d)): name for (m, d), name in _TO_MONDAY.items()})
    easter = _easter(year)
    days.update({easter + timedelta(days=n): name for n, name in _FROM_EASTER.items()})
    days.update({_next_monday(easter + timedelta(days=n)): name for n, name in _FROM_EASTER_TO_MONDAY.items()})
    return days


def holiday_name(day: date) -> Optional[str]:
    return holidays(day.year).get(day)

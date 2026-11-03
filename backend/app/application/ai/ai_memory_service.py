"""Lo que un buen vendedor recuerda de cada cliente y saca a relucir sin que se note que lo anotó."""
from __future__ import annotations

import re
from typing import Any, Optional

from app.domain.entities import Conversation

MAX_NOTES = 12
NOTE_MAX_CHARS = 120
# Teléfonos, cédulas, cuentas o tarjetas: no son detalles para recordar en una charla.
_SENSITIVE = re.compile(r"\d[\d .-]{6,}\d")

NOTES_RULE = """
notes = lo que vale la pena recordar de este cliente para el resto de la charla y para cuando vuelva:
personas y sus nombres (la hija Sofía, la esposa), para qué es y la ocasión, fecha y lugar, gustos o
temática, presupuesto, lo que le preocupa, si es sorpresa. Frases cortas, sin inventar nada.
Si en sus últimos mensajes no dijo nada nuevo que valga la pena, null. Si dijo algo, devuelve la lista
COMPLETA actualizada (lo que ya sabías + lo nuevo, corrigiendo lo que cambió). Nunca anotes números de
documento, teléfono, cuentas o tarjetas.
"""

_NOTES_BLOCK = """

Lo que ya sabes de este cliente (te lo contó antes, en este chat o en otra conversación):
{lines}
- Úsalo como un vendedor que se acuerda: solo cuando venga al caso, máximo un detalle por mensaje y metido
  con naturalidad (ej. «para el cumple de Sofi, la opción 3 le queda perfecta»).
- Nunca digas «como me dijiste», «recuerdo que», «según lo que me contaste» ni que tienes notas.
- No repitas el mismo detalle en mensajes seguidos, y no le vuelvas a preguntar lo que ya está aquí."""


def parse_notes(raw: Any) -> Optional[tuple[str, ...]]:
    """None = la IA no aprendió nada nuevo y se conservan las notas que había."""
    if not isinstance(raw, list):
        return None
    notes: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        note = " ".join(item.split()).strip(" -•")[:NOTE_MAX_CHARS]
        key = note.lower()
        if not note or key in seen or key in {"null", "none"} or _SENSITIVE.search(note):
            continue
        seen.add(key)
        notes.append(note)
    return tuple(notes[:MAX_NOTES]) or None


def notes_of(conversation: Conversation) -> list[str]:
    return [line for line in (conversation.client_notes or "").splitlines() if line.strip()]


def notes_prompt_block(conversation: Conversation) -> str:
    notes = notes_of(conversation)
    if not notes:
        return ""
    return _NOTES_BLOCK.format(lines="\n".join(f"- {note}" for note in notes))


def remember(conversation: Conversation, notes: Optional[tuple[str, ...]]) -> bool:
    if not notes:
        return False
    text = "\n".join(notes)
    if text == (conversation.client_notes or ""):
        return False
    conversation.client_notes = text
    return True

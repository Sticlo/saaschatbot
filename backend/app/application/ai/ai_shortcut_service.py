from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.orm import Session

from app.application.outbound.quick_shortcut_service import find_shortcut
from app.application.whatsapp.whatsapp_service import send_image_message, send_text_message
from app.domain.entities import Conversation, Tenant, WhatsAppSession

if TYPE_CHECKING:
    from app.application.ai.ai_appointment_service import BookSlotRequest

log = logging.getLogger(__name__)

_SHORTCUT_PREVIEW_LEN = 120

_JSON_REPLY_INSTRUCTION = """
Atajos rápidos que puedes enviar al cliente (botones con contenido listo):
{shortcut_lines}

Cuando el cliente pida menú, fotos, precios, catálogo o algo que coincida con un atajo,
responde con un mensaje breve y envía ese atajo (shortcut_id).
Si no aplica ningún atajo, usa shortcut_id null.

Responde SOLO con JSON válido (sin markdown), exactamente:
{{"message":"tu respuesta al cliente","shortcut_id":null}}
o
{{"message":"texto breve antes del atajo","shortcut_id":"id-del-atajo"}}

Reglas:
- Usa solo shortcut_id de la lista de arriba. No inventes ids.
- No repitas en message el contenido completo del atajo; el atajo se envía aparte.
- Si ningún atajo aplica, shortcut_id debe ser null.
"""


@dataclass(frozen=True)
class AiGeneratedReply:
    message: str
    shortcut_id: Optional[str] = None
    book_slot: Optional["BookSlotRequest"] = None


def _preview_text(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= _SHORTCUT_PREVIEW_LEN:
        return cleaned
    return cleaned[: _SHORTCUT_PREVIEW_LEN - 1] + "…"


def format_shortcut_line(shortcut: dict[str, Any]) -> str:
    sid = str(shortcut.get("id") or "")
    label = str(shortcut.get("label") or "").strip()
    kind = str(shortcut.get("type") or "text").lower()
    if kind == "image":
        content = f"imagen ({label})"
    else:
        content = _preview_text(str(shortcut.get("text") or ""))
    return f'- id="{sid}" | botón="{label}" | tipo={kind} | contenido: {content}'


def append_shortcuts_instructions(base_prompt: str, shortcuts: list[dict]) -> str:
    if not shortcuts:
        return base_prompt
    lines = [format_shortcut_line(s) for s in shortcuts if s.get("id")]
    if not lines:
        return base_prompt
    block = _JSON_REPLY_INSTRUCTION.format(shortcut_lines="\n".join(lines))
    return base_prompt.rstrip() + "\n" + block


def shortcut_ids(shortcuts: list[dict]) -> frozenset[str]:
    return frozenset(str(s.get("id") or "") for s in shortcuts if s.get("id"))


def _strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def parse_ai_reply(
    raw: str,
    *,
    valid_ids: frozenset[str],
    valid_book_keys: frozenset[tuple[str, str, str]] | None = None,
) -> AiGeneratedReply:
    from app.application.ai.ai_appointment_service import parse_book_slot

    text = (raw or "").strip()
    if not text:
        return AiGeneratedReply(message="")

    book_keys = valid_book_keys or frozenset()
    needs_json = bool(valid_ids) or bool(book_keys)
    if not needs_json:
        return AiGeneratedReply(message=text)

    candidate = _strip_json_fence(text)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{[^{}]*\"message\"[^{}]*\}", candidate, re.DOTALL)
        if not match:
            return AiGeneratedReply(message=text)
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return AiGeneratedReply(message=text)

    if not isinstance(data, dict):
        return AiGeneratedReply(message=text)

    message = str(data.get("message") or data.get("reply") or "").strip()
    if not message:
        return AiGeneratedReply(message=text)

    raw_sid = data.get("shortcut_id")
    shortcut_id: Optional[str] = None
    if raw_sid is not None:
        sid = str(raw_sid).strip()
        if sid and sid.lower() not in {"null", "none"} and sid in valid_ids:
            shortcut_id = sid

    book_slot = parse_book_slot(data.get("book_slot"), valid_keys=book_keys)

    return AiGeneratedReply(message=message, shortcut_id=shortcut_id, book_slot=book_slot)


def send_bot_shortcut(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    shortcut: dict,
) -> None:
    from app.domain.entities.enums import MessageSource

    if shortcut.get("type") == "image":
        send_image_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            image_path=str(shortcut["image_path"]),
            caption="",
            source=MessageSource.BOT.value,
        )
        return

    send_text_message(
        db,
        tenant=tenant,
        session=session,
        conversation=conversation,
        text=str(shortcut["text"]),
        source=MessageSource.BOT.value,
    )


def send_reply_with_shortcut(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    reply: AiGeneratedReply,
) -> None:
    """Envía mensaje de la IA y atajo opcional — sin cerrar venta ni agendar."""
    from app.domain.entities.enums import MessageSource

    if not reply.message.strip():
        return

    send_text_message(
        db,
        tenant=tenant,
        session=session,
        conversation=conversation,
        text=reply.message.strip(),
        source=MessageSource.BOT.value,
    )

    if not reply.shortcut_id:
        return

    shortcut = find_shortcut(db, tenant.id, reply.shortcut_id)
    if shortcut is None:
        log.warning(
            "IA pidió atajo inexistente tenant=%s shortcut_id=%s",
            tenant.id,
            reply.shortcut_id,
        )
        return

    try:
        send_bot_shortcut(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            shortcut=shortcut,
        )
        log.info(
            "IA envió atajo tenant=%s conv=%s label=%s",
            tenant.id,
            conversation.id,
            shortcut.get("label"),
        )
    except Exception:
        log.exception(
            "Error enviando atajo IA conv=%s shortcut=%s",
            conversation.id,
            reply.shortcut_id,
        )

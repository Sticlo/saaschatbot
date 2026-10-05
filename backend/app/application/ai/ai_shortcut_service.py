from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.orm import Session

from app.application.outbound.quick_shortcut_service import find_shortcut
from app.application.whatsapp.whatsapp_service import (
    send_document_message,
    send_image_message,
    send_text_message,
)
from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
from app.infrastructure.cache.redis_client import get_redis

if TYPE_CHECKING:
    from app.application.ai.ai_appointment_service import BookSlotRequest

log = logging.getLogger(__name__)

_SHORTCUT_PREVIEW_LEN = 120

_CATALOG_KNOWLEDGE_CHARS = 6000
SHORTCUT_SENT_TTL_SECONDS = 7 * 24 * 3600
SHORTCUT_RESEND_GUARD_SECONDS = 30 * 60

_JSON_REPLY_INSTRUCTION = """
Atajos rápidos que puedes enviar al cliente (botones con contenido listo):
{shortcut_lines}
{catalog_block}
Cuando el cliente pida menú, carta, fotos, precios, catálogo o algo que coincida con un atajo,
responde con un mensaje breve y envía ese atajo (shortcut_id).
Si un atajo dice "ya enviado en este chat", NO lo vuelvas a enviar: responde con la
información que conoces o recuérdale que se lo compartiste arriba. Solo reenvíalo si el
cliente lo pide otra vez de forma explícita (por ejemplo "mándamelo de nuevo").
- Usa solo shortcut_id de la lista de arriba. No inventes ids.
- No repitas en message el contenido completo del atajo; el atajo se envía aparte.
- Si ningún atajo aplica, shortcut_id debe ser null.
"""

STAGE_CHATTING = "conversando"
STAGE_INTERESTED = "interesado"
STAGE_CLOSING = "cierre"
STAGE_RANK = {STAGE_CHATTING: 0, STAGE_INTERESTED: 1, STAGE_CLOSING: 2}

_REPLY_FORMAT = """

Responde SOLO con JSON válido (sin markdown), exactamente con estas claves:
{{"message":"tu respuesta al cliente","shortcut_id":null,"book_slot":null,"stage":"conversando"}}
{key_rules}
stage = en qué punto está el cliente, mirando toda la conversación:
- "conversando": saluda, charla o pregunta en general; aún no muestra intención de compra.
- "interesado": muestra interés real: pide el catálogo, la carta o fotos; pregunta precio, talla,
  color o disponibilidad de algo concreto; pregunta por envíos o domicilios.
- "cierre": ya decidió: quiere pedir, pagar, apartar o reservar, pide datos para pagar o la
  dirección para ir, o confirma que lo lleva.
Si el stage es "cierre": responde cálido y natural, como alguien del negocio; confirma lo que
quiere y dile que ya le confirmas en un momentico (ej. «¡De una! Dame un momentico y te
confirmo 🙌»). No digas que lo pasas con un asesor u otra persona ni que eres un bot.
No pidas datos de pago."""


def reply_format_instruction(*, has_shortcuts: bool, booking_enabled: bool) -> str:
    rules = []
    if not has_shortcuts:
        rules.append("- shortcut_id siempre null (no hay atajos).")
    if not booking_enabled:
        rules.append(
            "- book_slot siempre null. Si piden una cita o reserva, no la confirmes tú: es stage \"cierre\"."
        )
    key_rules = ("\n".join(rules) + "\n") if rules else ""
    return _REPLY_FORMAT.format(key_rules=key_rules)

_CATALOG_BLOCK = """
Lo que sabes de los productos (sacado del catálogo/carta del negocio):
{catalog_text}

Usa esta información para responder preguntas concretas (precios, tallas, sabores,
disponibilidad). No inventes productos ni precios que no estén aquí.
"""


@dataclass(frozen=True)
class AiGeneratedReply:
    message: str
    shortcut_id: Optional[str] = None
    book_slot: Optional["BookSlotRequest"] = None
    stage: str = STAGE_CHATTING


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
    elif kind == "document":
        content = f"archivo PDF ({shortcut.get('file_name') or label})"
    else:
        content = _preview_text(str(shortcut.get("text") or ""))
    line = f'- id="{sid}" | botón="{label}" | tipo={kind} | contenido: {content}'
    if shortcut.get("already_sent"):
        line += " | ya enviado en este chat"
    return line


def _catalog_text(shortcuts: list[dict]) -> str:
    parts: list[str] = []
    remaining = _CATALOG_KNOWLEDGE_CHARS
    for shortcut in shortcuts:
        if shortcut.get("type") not in ("image", "document"):
            continue
        content = str(shortcut.get("content") or "").strip()
        if not content or remaining <= 0:
            continue
        label = str(shortcut.get("label") or "").strip() or "Catálogo"
        chunk = f"[{label}]\n{content[:remaining]}"
        parts.append(chunk)
        remaining -= len(chunk)
    return "\n\n".join(parts)


def append_shortcuts_instructions(base_prompt: str, shortcuts: list[dict]) -> str:
    if not shortcuts:
        return base_prompt
    lines = [format_shortcut_line(s) for s in shortcuts if s.get("id")]
    if not lines:
        return base_prompt
    catalog_text = _catalog_text(shortcuts)
    catalog_block = _CATALOG_BLOCK.format(catalog_text=catalog_text) if catalog_text else ""
    block = _JSON_REPLY_INSTRUCTION.format(
        shortcut_lines="\n".join(lines), catalog_block=catalog_block
    )
    return base_prompt.rstrip() + "\n" + block


def _sent_key(conversation_id, shortcut_id: str) -> str:
    return f"ai:shortcut_sent:{conversation_id}:{shortcut_id}"


def mark_shortcut_sent(conversation_id, shortcut_id: str) -> None:
    try:
        get_redis().set(
            _sent_key(conversation_id, shortcut_id),
            str(int(time.time())),
            ex=SHORTCUT_SENT_TTL_SECONDS,
        )
    except Exception:
        log.warning("No se pudo marcar atajo enviado conv=%s", conversation_id, exc_info=True)


def shortcut_sent_at(conversation_id, shortcut_id: str) -> Optional[float]:
    try:
        raw = get_redis().get(_sent_key(conversation_id, shortcut_id))
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def annotate_sent_shortcuts(shortcuts: list[dict], conversation_id) -> list[dict]:
    annotated = []
    for shortcut in shortcuts:
        item = dict(shortcut)
        sid = str(item.get("id") or "")
        if sid and shortcut_sent_at(conversation_id, sid) is not None:
            item["already_sent"] = True
        annotated.append(item)
    return annotated


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
    data = _load_reply_json(_strip_json_fence(text))
    if data is None:
        # JSON roto: nunca mandarle llaves y comillas al cliente, solo el texto del mensaje.
        return AiGeneratedReply(message=_salvage_message(text) or text)

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
    stage = str(data.get("stage") or "").strip().lower()
    if stage not in STAGE_RANK:
        stage = STAGE_CHATTING

    return AiGeneratedReply(
        message=message, shortcut_id=shortcut_id, book_slot=book_slot, stage=stage
    )


def _load_reply_json(candidate: str) -> Optional[dict]:
    attempts = [candidate]
    start, end = candidate.find("{"), candidate.rfind("}")
    if 0 <= start < end:
        attempts.append(candidate[start : end + 1])
    for attempt in attempts:
        try:
            data = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _salvage_message(text: str) -> Optional[str]:
    match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"').strip() or None
    except json.JSONDecodeError:
        return None


def send_shortcut_content(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    shortcut: dict,
    source: str,
) -> Message:
    kind = shortcut.get("type")
    if kind == "image":
        message = send_image_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            image_path=str(shortcut["image_path"]),
            caption="",
            source=source,
        )
    elif kind == "document":
        message = send_document_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            file_path=str(shortcut["file_path"]),
            file_name=str(shortcut.get("file_name") or "catalogo.pdf"),
            source=source,
        )
    else:
        message = send_text_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            text=str(shortcut["text"]),
            source=source,
        )
    if shortcut.get("id"):
        mark_shortcut_sent(message.conversation_id, str(shortcut["id"]))
    return message


def send_bot_shortcut(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    shortcut: dict,
) -> None:
    from app.domain.entities.enums import MessageSource

    send_shortcut_content(
        db,
        tenant=tenant,
        session=session,
        conversation=conversation,
        shortcut=shortcut,
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

    sent_at = shortcut_sent_at(conversation.id, reply.shortcut_id)
    if sent_at is not None and time.time() - sent_at < SHORTCUT_RESEND_GUARD_SECONDS:
        log.info(
            "Atajo ya enviado hace poco, no se reenvía conv=%s shortcut=%s",
            conversation.id,
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

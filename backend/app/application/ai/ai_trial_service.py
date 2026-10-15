"""«Prueba tu IA»: el dueño escribe como cliente y ve lo que la IA respondería en WhatsApp,
con las mismas reglas, fotos y atajos, sin mandar nada ni tocar chats reales."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.application.ai.ai_appointment_service import BookingContext, without_wait_promise
from app.application.ai.ai_conversation_service import generate_reply
from app.application.ai.ai_knowledge_service import learned_answers_block
from app.application.ai.ai_qualify_service import payment_methods_of
from app.application.ai.ai_shortcut_service import attach_promised_media, without_internal_refs
from app.application.ai.ai_usage_service import check_daily_reply_quota
from app.application.appointments.appointment_service import BOGOTA, collect_upcoming_free_slots, staff_label
from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.application.outbound.quick_shortcut_service import get_shortcuts
from app.application.platform.tenant_overrides import feature_allowed
from app.application.products.product_service import find_by_ref, list_products
from app.domain.entities import Message, Tenant
from app.domain.entities.enums import MessageDirection, MessageSource
from app.infrastructure.cache.redis_client import get_redis

MAX_TRIES_PER_DAY = 60
MAX_TURNS = 20
BOOKING_DAYS_AHEAD = 14


class TrialError(Exception):
    pass


@dataclass
class TrialReply:
    reply: str
    photos: list[str] = field(default_factory=list)
    attachment: Optional[dict] = None
    notes: list[str] = field(default_factory=list)


def _tries_key(tenant_id: uuid.UUID) -> str:
    return f"ai:try:{tenant_id}:{datetime.now(BOGOTA).date().isoformat()}"


def _tried_key(tenant_id: uuid.UUID) -> str:
    return f"ai:tried:{tenant_id}"


def has_tried(tenant_id: uuid.UUID) -> bool:
    try:
        return bool(get_redis().exists(_tried_key(tenant_id)))
    except Exception:
        return True


def _take_try(tenant_id: uuid.UUID) -> None:
    r = get_redis()
    used = r.incr(_tries_key(tenant_id))
    if used == 1:
        r.expire(_tries_key(tenant_id), 2 * 24 * 3600)
    if used > MAX_TRIES_PER_DAY:
        raise TrialError("Ya hiciste muchas pruebas hoy. Mañana puedes seguir probando.")
    r.set(_tried_key(tenant_id), "1")


def _history(turns: list[dict]) -> list[Message]:
    start = datetime.now(timezone.utc) - timedelta(minutes=len(turns))
    rows = []
    for i, turn in enumerate(turns[-MAX_TURNS:]):
        client = turn.get("from") == "client"
        rows.append(
            Message(
                direction=MessageDirection.IN.value if client else MessageDirection.OUT.value,
                source=MessageSource.CONTACT.value if client else MessageSource.BOT.value,
                body=str(turn.get("text") or "")[:1000],
                created_at=start + timedelta(minutes=i),
            )
        )
    return rows


def _client_turn(history: list[Message]) -> str:
    texts: list[str] = []
    for m in reversed(history):
        if m.direction != MessageDirection.IN.value:
            break
        texts.append(m.body or "")
    return "\n".join(reversed(texts))


def _booking(db: Session, tenant: Tenant, profile) -> Optional[BookingContext]:
    if not profile.ai_booking_enabled or not feature_allowed(tenant, "ai_booking"):
        return None
    today = datetime.now(BOGOTA).date()
    return BookingContext(
        free_slots=collect_upcoming_free_slots(db, tenant_id=tenant.id, days=BOOKING_DAYS_AHEAD, limit=None),
        staff_label=staff_label(profile),
        covered_until=(today + timedelta(days=BOOKING_DAYS_AHEAD - 1)).isoformat(),
    )


def _attachment(shortcut: dict) -> dict:
    kind = shortcut.get("type")
    row = {"label": shortcut.get("label") or "", "type": kind}
    if kind == "text":
        row["text"] = shortcut.get("text") or ""
    elif kind == "image":
        row["url"] = f"/api/v1/outbound/assets/{str(shortcut.get('image_path') or '').split('/')[-1]}"
    else:
        row["file_name"] = shortcut.get("file_name") or "catalogo.pdf"
    return row


def try_reply(db: Session, tenant: Tenant, turns: list[dict]) -> TrialReply:
    history = _history(turns)
    client_text = _client_turn(history)
    if not client_text.strip():
        raise TrialError("Escribe un mensaje como si fueras un cliente.")
    ok, _, _, err = check_daily_reply_quota(db, tenant)
    if not ok:
        raise TrialError(err or "Tu plan no incluye respuestas de la IA.")
    _take_try(tenant.id)

    profile = get_or_create_tenant_profile(db, tenant.id)
    products = list_products(db, tenant.id)
    shortcuts = get_shortcuts(db, tenant.id)
    booking = _booking(db, tenant, profile)
    reply = generate_reply(
        tenant=tenant,
        profile=profile,
        contact_name="",
        history=history,
        shortcuts=shortcuts,
        booking=booking,
        products=products,
        knowledge=learned_answers_block(db, tenant.id),
    )
    if not reply.message.strip():
        raise TrialError("La IA no respondió. Intenta otra vez en un momento.")
    recent = tuple(m.body or "" for m in reversed(history[:-1]))
    reply = attach_promised_media(
        reply, client_text=client_text, products=products, shortcuts=shortcuts, recent=recent
    )

    result = TrialReply(reply=without_internal_refs(without_wait_promise(reply.message), products))
    if reply.photo_ref:
        product = find_by_ref(products, reply.photo_ref)
        if product is not None:
            result.photos = [f"/api/v1/outbound/assets/{p.rsplit('/', 1)[-1]}" for p in product.photos or []]
    if reply.shortcut_id:
        shortcut = next((s for s in shortcuts if str(s.get("id")) == reply.shortcut_id), None)
        if shortcut is not None:
            result.attachment = _attachment(shortcut)
    if reply.missing:
        result.notes.append(
            f"La IA no sabía esto: «{reply.missing}». Respóndelo en «Preguntas que la IA no supo responder» y no vuelve a pasar."
        )
    if reply.book_slot is not None:
        result.notes.append(
            f"En un chat real quedaría agendada la cita del {reply.book_slot.date} a las {reply.book_slot.start}."
        )
    if reply.order is not None and payment_methods_of(profile):
        result.notes.append("En un chat real te llegaría el aviso del pedido y quedaría en Pedidos.")
    return result

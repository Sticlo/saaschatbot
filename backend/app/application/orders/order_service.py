"""Los pedidos que cierra la IA, para que el dueño solo despache: esperando pago, listo para
despachar y despachado. La IA repite el pedido en cada respuesta: se actualiza el mismo, no se duplica."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.application.ai.ai_shortcut_service import OrderSummary
from app.domain.entities import Conversation, Order, Tenant

log = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_DISPATCHED = "dispatched"
STATUS_CANCELLED = "cancelled"
STATUSES = (STATUS_PENDING, STATUS_READY, STATUS_DISPATCHED, STATUS_CANCELLED)
OPEN_STATUSES = (STATUS_PENDING, STATUS_READY)
STATUS_LABELS = {
    STATUS_PENDING: "esperando el pago",
    STATUS_READY: "pagado, listo para despachar",
    STATUS_DISPATCHED: "despachado",
    STATUS_CANCELLED: "cancelado",
}
# Tras despachar, la IA puede seguir repitiendo el mismo pedido en sus respuestas.
REPEAT_WINDOW = timedelta(days=3)
DISPATCHED_SHOWN = timedelta(days=30)
_CASH_ON_DELIVERY_RE = re.compile(
    r"contra\s*-?\s*entrega|al recibir|cuando (le|me|te) llegue|pago en (el )?(local|tienda)|paga(rá)? al recoger",
    re.IGNORECASE,
)


class OrderError(ValueError):
    pass


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w$]+", " ", (text or "").lower()).split())


def is_cash_on_delivery(payment: str) -> bool:
    return bool(_CASH_ON_DELIVERY_RE.search(payment or ""))


def record_ai_order(db: Session, tenant: Tenant, conversation: Conversation, summary: OrderSummary) -> Optional[Order]:
    """Crea o actualiza el pedido abierto del chat. None si es el eco de uno ya cerrado."""
    latest = (
        db.query(Order)
        .filter(Order.tenant_id == tenant.id, Order.conversation_id == conversation.id)
        .order_by(Order.created_at.desc())
        .first()
    )
    now = datetime.now(timezone.utc)
    if latest is not None and latest.status not in OPEN_STATUSES:
        if _norm(latest.items) == _norm(summary.items) and now - latest.created_at < REPEAT_WINDOW:
            return None
        latest = None

    order = latest
    if order is None:
        order = Order(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            contact_name=conversation.contact_name or "",
            contact_phone=conversation.contact_phone or "",
            items=summary.items,
            status=STATUS_PENDING,
        )
        db.add(order)
    order.items = summary.items
    for field in ("total", "customer", "delivery", "payment"):
        value = getattr(summary, field)
        if value:
            setattr(order, field, value)
    if summary.paid:
        order.payment_proof = True
    if order.status == STATUS_PENDING and (order.payment_proof or is_cash_on_delivery(order.payment)):
        order.status = STATUS_READY
    db.flush()
    return order


def get_order(db: Session, tenant_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    order = db.query(Order).filter(Order.tenant_id == tenant_id, Order.id == order_id).first()
    if order is None:
        raise LookupError("Pedido no encontrado")
    return order


def list_orders(db: Session, tenant_id: uuid.UUID) -> list[Order]:
    """Los abiertos y lo despachado o cancelado del último mes."""
    since = datetime.now(timezone.utc) - DISPATCHED_SHOWN
    return (
        db.query(Order)
        .filter(
            Order.tenant_id == tenant_id,
            (Order.status.in_(OPEN_STATUSES)) | (Order.updated_at >= since),
        )
        .order_by(Order.created_at.desc())
        .limit(300)
        .all()
    )


def dispatch_message(order: Order) -> str:
    name = (order.customer or order.contact_name or "").split(" ")[0]
    if name.startswith("+"):
        name = ""
    text = f"¡Hola{' ' + name if name else ''}! Tu pedido ya va en camino: {order.items}."
    if order.tracking:
        text += f" Número de guía: {order.tracking}."
    return text + " Cualquier cosa me escribes por aquí."


def set_status(
    db: Session,
    tenant: Tenant,
    order: Order,
    status: str,
    *,
    tracking: str = "",
    notify_customer: bool = True,
) -> bool:
    """Cambia el estado. Al despachar le avisa al cliente por WhatsApp. True si se le avisó."""
    if status not in STATUSES:
        raise OrderError("Estado inválido")
    order.status = status
    if tracking.strip():
        order.tracking = tracking.strip()[:120]
    if status != STATUS_DISPATCHED:
        order.dispatched_at = None
        db.flush()
        return False
    order.dispatched_at = datetime.now(timezone.utc)
    db.flush()
    if not notify_customer or order.conversation_id is None:
        return False
    return _notify_dispatched(db, tenant, order)


def _notify_dispatched(db: Session, tenant: Tenant, order: Order) -> bool:
    from app.application.whatsapp.whatsapp_service import send_text_message
    from app.domain.entities.enums import MessageSource

    conversation = db.get(Conversation, order.conversation_id)
    session = tenant.whatsapp_session
    if conversation is None or session is None:
        return False
    try:
        # Como la IA: si saliera como mensaje del dueño, la IA se apartaría del chat.
        send_text_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            text=dispatch_message(order),
            source=MessageSource.BOT.value,
        )
    except Exception:
        log.warning("No se pudo avisar el despacho order=%s", order.id, exc_info=True)
        return False
    return True


def orders_prompt_block(db: Session, conversation_id: uuid.UUID) -> str:
    """Para que la IA sepa qué pasó con lo que el cliente compró si pregunta «¿ya lo enviaron?»."""
    rows = (
        db.query(Order)
        .filter(Order.conversation_id == conversation_id, Order.status != STATUS_CANCELLED)
        .order_by(Order.created_at.desc())
        .limit(3)
        .all()
    )
    lines: list[str] = []
    for row in rows:
        line = f"- {row.items}: {STATUS_LABELS.get(row.status, row.status)}"
        if row.status == STATUS_READY and row.payment_proof:
            line = f"- {row.items}: llegó el comprobante; el negocio lo revisa y lo despacha"
        if row.tracking:
            line += f" (guía {row.tracking})"
        lines.append(line)
    if not lines:
        return ""
    return (
        "\n\nPedidos de este cliente (el más reciente primero). Si pregunta por su pedido, respóndele "
        "con esto; no prometas fechas de entrega que no estén aquí:\n" + "\n".join(lines)
    )


def has_recent_purchase(db: Session, conversation_id: uuid.UUID, since: datetime) -> bool:
    return (
        db.query(Order.id)
        .filter(
            Order.conversation_id == conversation_id,
            Order.status.in_((STATUS_READY, STATUS_DISPATCHED)),
            Order.updated_at >= since,
        )
        .first()
        is not None
    )


def order_to_dict(order: Order) -> dict:
    return {
        "id": str(order.id),
        "conversation_id": str(order.conversation_id) if order.conversation_id else None,
        "contact_name": order.contact_name,
        "contact_phone": order.contact_phone,
        "items": order.items,
        "total": order.total,
        "customer": order.customer,
        "delivery": order.delivery,
        "payment": order.payment,
        "payment_proof": order.payment_proof,
        "status": order.status,
        "tracking": order.tracking,
        "dispatched_at": order.dispatched_at.isoformat() if order.dispatched_at else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }

from __future__ import annotations

import logging
import threading
import uuid
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import Conversation, Message, Tenant, TenantProfile, WhatsAppSession
from app.domain.entities.enums import ConversationInterest, MessageSource, WhatsAppStatus
from app.infrastructure.cache.redis_client import get_redis, tenant_cache_key
from app.infrastructure.persistence.database import SessionLocal
from app.shared.core.phone import (
    is_valid_whatsapp_phone,
    normalize_phone,
    phone_match_tail,
    phone_to_evolution_number,
    resolve_display_name,
)

log = logging.getLogger(__name__)

DEFAULT_ALERT_THRESHOLD = 10
ALERT_REPEAT_SECONDS = 24 * 3600
SWEEP_INTERVAL_SECONDS = 60
MAX_NAMES_IN_ALERT = 5

_sweeper_thread: Optional[threading.Thread] = None
_sweeper_stop = threading.Event()


class InterestAlertError(Exception):
    pass


def normalize_alert_phone(raw: str) -> str:
    """'' borra el número; si no es un WhatsApp válido lanza InterestAlertError."""
    if not (raw or "").strip():
        return ""
    phone = normalize_phone(raw)
    if not is_valid_whatsapp_phone(phone):
        raise InterestAlertError("Número inválido — escríbelo con indicativo, ej. +57 300 123 4567")
    return phone


def is_alert_phone(tenant: Tenant, phone: str) -> bool:
    alert_phone = tenant.profile.alert_phone if tenant.profile else None
    return bool(alert_phone and phone and phone_match_tail(alert_phone, phone))


def unanswered_interested_conversations(db: Session, tenant_id: uuid.UUID) -> list[Conversation]:
    """Interesados cuyo último mensaje del cliente no tiene respuesta de un humano después."""

    def last_from(source: str):
        return (
            select(func.max(Message.created_at))
            .where(Message.conversation_id == Conversation.id, Message.source == source)
            .correlate(Conversation)
            .scalar_subquery()
        )

    last_contact = last_from(MessageSource.CONTACT.value)
    last_agent = last_from(MessageSource.AGENT.value)
    return (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.interest_status == ConversationInterest.INTERESTED.value,
            Conversation.is_archived.is_(False),
            last_contact.isnot(None),
            or_(last_agent.is_(None), last_agent < last_contact),
        )
        .order_by(Conversation.last_message_at.desc().nullslast())
        .all()
    )


def build_alert_text(business_name: str, pending: list[Conversation]) -> str:
    names = [
        resolve_display_name(c.contact_name, c.contact_phone, contact_jid=c.contact_jid or "")
        for c in pending[:MAX_NAMES_IN_ALERT]
    ]
    lines = [
        f"🔔 *{business_name}*: tienes {len(pending)} clientes interesados esperando respuesta.",
        "",
        *[f"• {name}" for name in names],
    ]
    if len(pending) > len(names):
        lines.append(f"…y {len(pending) - len(names)} más.")
    lines += ["", f"Respóndeles aquí: {settings.app_public_url.rstrip('/')}/panel"]
    return "\n".join(lines)


def send_alert(session: WhatsAppSession, alert_phone: str, text: str) -> None:
    from app.application.whatsapp.whatsapp_gateway import send_text

    send_text(session.instance_name, phone_to_evolution_number(alert_phone), text)


def _alert_key(tenant_id: uuid.UUID) -> str:
    return tenant_cache_key(str(tenant_id), "interest_alert_sent")


def check_interest_backlog(db: Session, tenant: Tenant) -> bool:
    """Un aviso al cruzar el umbral; se repite cada 24 h mientras siga el atraso.
    Si el atraso baja del umbral, el próximo cruce vuelve a avisar. True = aviso enviado."""
    profile = tenant.profile
    if profile is None or not profile.alert_phone:
        return False
    if tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value or tenant.whatsapp_session is None:
        return False

    pending = unanswered_interested_conversations(db, tenant.id)
    threshold = profile.alert_threshold or DEFAULT_ALERT_THRESHOLD
    client = get_redis()
    key = _alert_key(tenant.id)
    if len(pending) < threshold:
        client.delete(key)
        return False
    # nx evita avisos duplicados si hay varios workers de IA.
    if not client.set(key, "1", nx=True, ex=ALERT_REPEAT_SECONDS):
        return False

    try:
        send_alert(
            tenant.whatsapp_session,
            profile.alert_phone,
            build_alert_text(tenant.business_name, pending),
        )
    except Exception:
        client.delete(key)
        log.exception("No se pudo enviar alerta de interesados tenant=%s", tenant.id)
        return False
    log.info("Alerta de interesados enviada tenant=%s pendientes=%s", tenant.id, len(pending))
    return True


def _sweep_once() -> None:
    db = SessionLocal()
    try:
        tenants = (
            db.query(Tenant)
            .join(TenantProfile, TenantProfile.tenant_id == Tenant.id)
            .filter(
                Tenant.is_active.is_(True),
                Tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value,
                TenantProfile.alert_phone.isnot(None),
            )
            .all()
        )
        for tenant in tenants:
            try:
                check_interest_backlog(db, tenant)
            except Exception:
                log.exception("Error revisando interesados tenant=%s", tenant.id)
    finally:
        db.close()


def _sweeper_loop() -> None:
    log.info("Interest alert sweeper started")
    while not _sweeper_stop.wait(SWEEP_INTERVAL_SECONDS):
        try:
            _sweep_once()
        except Exception:
            log.exception("Interest alert sweeper error")
    log.info("Interest alert sweeper stopped")


def start_interest_alert_sweeper() -> None:
    global _sweeper_thread
    if _sweeper_thread and _sweeper_thread.is_alive():
        return
    _sweeper_stop.clear()
    _sweeper_thread = threading.Thread(
        target=_sweeper_loop,
        name="interest-alert-sweeper",
        daemon=True,
    )
    _sweeper_thread.start()


def stop_interest_alert_sweeper() -> None:
    _sweeper_stop.set()
    if _sweeper_thread and _sweeper_thread.is_alive():
        _sweeper_thread.join(timeout=1)

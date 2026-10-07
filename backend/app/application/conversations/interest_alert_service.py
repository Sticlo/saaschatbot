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
MAX_ALERT_RECIPIENTS = 5

# "all": dueño/encargado — todas las alertas. "sales": quien despacha — solo ventas listas y citas.
SCOPE_ALL = "all"
SCOPE_SALES = "sales"
ALERT_SCOPES = (SCOPE_ALL, SCOPE_SALES)

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


def normalize_alert_recipients(items: list[dict]) -> list[dict]:
    """Filas sin número se descartan; números repetidos cuentan una sola vez."""
    recipients: list[dict] = []
    for item in items:
        name = " ".join(str(item.get("name") or "").split())[:60]
        raw_phone = str(item.get("phone") or "")
        try:
            phone = normalize_alert_phone(raw_phone)
        except InterestAlertError:
            who = name or raw_phone.strip()
            raise InterestAlertError(
                f"Número inválido ({who}) — escríbelo con indicativo, ej. +57 300 123 4567"
            )
        if not phone or any(phone_match_tail(r["phone"], phone) for r in recipients):
            continue
        scope = item.get("scope") if item.get("scope") in ALERT_SCOPES else SCOPE_ALL
        recipients.append({"name": name, "phone": phone, "scope": scope})
    if len(recipients) > MAX_ALERT_RECIPIENTS:
        raise InterestAlertError(f"Máximo {MAX_ALERT_RECIPIENTS} números para alertas")
    return recipients


def alert_recipients(profile: Optional[TenantProfile]) -> list[dict]:
    if profile is None:
        return []
    return [r for r in (profile.alert_recipients or []) if isinstance(r, dict) and r.get("phone")]


def alert_phones(profile: Optional[TenantProfile], *, sales: bool = False) -> list[str]:
    """sales=True: ventas listas y citas (todo el equipo). False: solo quienes reciben todo."""
    return [
        r["phone"]
        for r in alert_recipients(profile)
        if sales or r.get("scope", SCOPE_ALL) == SCOPE_ALL
    ]


def is_alert_phone(tenant: Tenant, phone: str) -> bool:
    if not phone:
        return False
    return any(phone_match_tail(p, phone) for p in alert_phones(tenant.profile, sales=True))


def unanswered_interested_conversations(db: Session, tenant_id: uuid.UUID) -> list[Conversation]:
    """Interesados cuyo último mensaje del cliente no tiene respuesta de un humano después."""

    def last_from(source: str, *extra):
        return (
            select(func.max(Message.created_at))
            .where(Message.conversation_id == Conversation.id, Message.source == source, *extra)
            .correlate(Conversation)
            .scalar_subquery()
        )

    # Mismo criterio que is_reaction_only: un ❤️ o 👍 del cliente no lo deja esperando respuesta.
    last_contact = last_from(
        MessageSource.CONTACT.value,
        Message.body.op("~")("[[:alnum:]]"),
        ~Message.body.ilike("[reaction%"),
    )
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


def send_alerts(session: WhatsAppSession, phones: list[str], text: str) -> int:
    """Un número que falle no impide avisar a los demás. Devuelve cuántos se enviaron."""
    sent = 0
    for phone in phones:
        try:
            send_alert(session, phone, text)
            sent += 1
        except Exception:
            log.warning("No se pudo enviar alerta a %s", phone[-4:], exc_info=True)
    return sent


def _alert_key(tenant_id: uuid.UUID) -> str:
    return tenant_cache_key(str(tenant_id), "interest_alert_sent")


def check_interest_backlog(db: Session, tenant: Tenant) -> bool:
    """Un aviso al cruzar el umbral; se repite cada 24 h mientras siga el atraso.
    Si el atraso baja del umbral, el próximo cruce vuelve a avisar. True = aviso enviado."""
    profile = tenant.profile
    phones = alert_phones(profile)
    if not phones:
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

    if not send_alerts(tenant.whatsapp_session, phones, build_alert_text(tenant.business_name, pending)):
        client.delete(key)
        log.error("No se pudo enviar alerta de interesados tenant=%s", tenant.id)
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
                TenantProfile.alert_recipients[0].isnot(None),
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

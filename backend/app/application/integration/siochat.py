"""Integración con siochat (prospección): de dónde llegó cada registro, en qué va y avisos firmados."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.application.billing.subscription_service import subscription_is_paid, trial_is_running
from app.application.conversations.interest_alert_service import business_phone
from app.config import settings
from app.domain.entities import (
    IntegrationEvent,
    Subscription,
    SubscriptionStatus,
    Tenant,
    TenantProfile,
    WhatsAppSession,
)
from app.domain.entities.payment_checkout import PaymentCheckout, PaymentCheckoutStatus
from app.infrastructure.persistence.database import SessionLocal

log = logging.getLogger(__name__)

ATTRIBUTION_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref")
MAX_ATTRIBUTION_CHARS = 100
EVENTS = (
    "registro",
    "whatsapp_conectado",
    "whatsapp_desconectado",
    "prueba_por_vencer",
    "pago",
    "vencido",
    "cancelado",
)
# Espera antes de cada reintento si siochat no responde 2xx; después se abandona.
RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))
SEND_TIMEOUT_SECONDS = 10
SENDER_INTERVAL_SECONDS = 30
SENDER_BATCH = 50


def normalize_phone(raw: Any) -> Optional[str]:
    """57 + 10 dígitos (o el número internacional completo), sin + ni espacios."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 10 and digits.startswith("3"):
        digits = "57" + digits
    return digits if 11 <= len(digits) <= 15 else None


def clean_attribution(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in ATTRIBUTION_KEYS:
        value = re.sub(r"[\x00-\x1f\x7f]", "", str(raw.get(key) or "")).strip()[:MAX_ATTRIBUTION_CHARS]
        if value:
            out[key] = value
    lead = normalize_phone(raw.get("lead"))
    if lead:
        out["lead"] = lead
    return out


def apply_attribution(tenant: Tenant, raw: Any) -> None:
    clean = clean_attribution(raw)
    if clean:
        tenant.signup_attribution = clean
        tenant.lead_phone = clean.get("lead")


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def account_state(tenant: Tenant, sub: Optional[Subscription], now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    if not tenant.is_active or (sub is not None and sub.status == SubscriptionStatus.CANCELLED.value):
        return "cancelado"
    if subscription_is_paid(sub, now):
        return "activo"
    if trial_is_running(sub, now):
        return "prueba"
    return "vencido"


def _session(db: Session, tenant_id) -> Optional[WhatsAppSession]:
    return db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()


def _subscription(db: Session, tenant_id) -> Optional[Subscription]:
    return db.query(Subscription).filter(Subscription.tenant_id == tenant_id).first()


def tenant_status(db: Session, tenant: Tenant) -> dict[str, Any]:
    """Lo que siochat necesita saber de un registro para cerrar su embudo."""
    sub = _subscription(db, tenant.id)
    session = _session(db, tenant.id)
    count, total, last = (
        db.query(
            func.count(PaymentCheckout.id),
            func.coalesce(func.sum(PaymentCheckout.amount_in_cents), 0),
            func.max(PaymentCheckout.paid_at),
        )
        .filter(PaymentCheckout.tenant_id == tenant.id, PaymentCheckout.status == PaymentCheckoutStatus.APPROVED)
        .one()
    )
    attribution = tenant.signup_attribution or {}
    plan = sub.plan.name if sub is not None and sub.plan is not None else None
    return {
        "registrado": True,
        "fecha_registro": _iso(tenant.created_at),
        "negocio": tenant.business_name,
        "estado": account_state(tenant, sub),
        "plan": plan,
        "fecha_vencimiento_prueba": _iso(sub.trial_ends_at) if sub is not None else None,
        "fecha_vencimiento_plan": _iso(sub.current_period_end) if sub is not None else None,
        "pagos": {"cantidad": int(count), "total_cop": int(total) // 100, "ultimo": _iso(last)},
        "lead": tenant.lead_phone,
        "telefono_whatsapp": normalize_phone(business_phone(session)),
        "whatsapp_conectado": tenant.whatsapp_status == "connected",
        "utm_source": attribution.get("utm_source"),
        "utm_medium": attribution.get("utm_medium"),
        "utm_campaign": attribution.get("utm_campaign"),
    }


def find_tenant_by_phone(db: Session, phone: str) -> Optional[Tenant]:
    """Primero el lead del link (exacto); si no, el WhatsApp conectado o un teléfono de alertas."""
    by_lead = (
        db.query(Tenant).filter(Tenant.lead_phone == phone).order_by(Tenant.created_at.desc()).first()
    )
    if by_lead is not None:
        return by_lead
    # Al desconectarse se borra phone_number; el JID del dueño vinculado se conserva.
    session = (
        db.query(WhatsAppSession)
        .filter(
            or_(
                WhatsAppSession.phone_number.in_({phone, f"+{phone}"}),
                WhatsAppSession.bound_owner_jid.like(f"{phone}@%"),
                WhatsAppSession.bound_owner_jid.like(f"{phone}:%"),
            )
        )
        .order_by(WhatsAppSession.updated_at.desc())
        .first()
    )
    if session is not None:
        return db.get(Tenant, session.tenant_id)
    for profile in db.query(TenantProfile).filter(TenantProfile.alert_recipients.isnot(None)):
        for recipient in profile.alert_recipients or []:
            if isinstance(recipient, dict) and normalize_phone(recipient.get("phone")) == phone:
                return db.get(Tenant, profile.tenant_id)
    return None


def integration_enabled() -> bool:
    return bool(settings.integration_webhook_url.strip() and settings.omitel_webhook_secret.strip())


def record_event(db: Session, tenant: Tenant, event: str, *, monto: Optional[int] = None) -> Optional[IntegrationEvent]:
    """Deja el aviso en la misma transacción que el cambio: si el cambio no se guarda, el aviso tampoco."""
    if event not in EVENTS or not integration_enabled():
        return None
    db.flush()
    status = tenant_status(db, tenant)
    row = IntegrationEvent(tenant_id=tenant.id, event=event, payload={})
    db.add(row)
    db.flush()
    row.payload = {
        "id": str(row.id),
        "evento": event,
        "fecha": _iso(datetime.now(timezone.utc)),
        "lead": status["lead"],
        "telefono_whatsapp": status["telefono_whatsapp"],
        "negocio": status["negocio"],
        "plan": status["plan"],
        "estado": status["estado"],
        "monto": monto,
        "utm_source": status["utm_source"],
        "utm_campaign": status["utm_campaign"],
    }
    return row


WHATSAPP_EVENTS = ("whatsapp_conectado", "whatsapp_desconectado")
TRIAL_ENDING_NOTICE = timedelta(hours=24)


def sweep_state_events(db: Session, now: Optional[datetime] = None) -> int:
    """Cambios que no pasan por un solo punto del código: WhatsApp y prueba a punto de vencer."""
    if not integration_enabled():
        return 0
    from app.application.whatsapp.whatsapp_health_service import OUTAGE_CONFIRM_SECONDS, down_started_at

    now = now or datetime.now(timezone.utc)
    last_whatsapp = dict(
        db.query(IntegrationEvent.tenant_id, IntegrationEvent.event)
        .filter(IntegrationEvent.event.in_(WHATSAPP_EVENTS))
        .order_by(IntegrationEvent.tenant_id, IntegrationEvent.created_at.desc())
        .distinct(IntegrationEvent.tenant_id)
        .all()
    )
    recorded = 0
    for tenant in db.query(Tenant).filter(Tenant.is_active.is_(True)):
        previous = last_whatsapp.get(tenant.id)
        if tenant.whatsapp_status == "connected":
            if previous != "whatsapp_conectado":
                record_event(db, tenant, "whatsapp_conectado")
                recorded += 1
        elif previous == "whatsapp_conectado":
            started = down_started_at(tenant.id)
            if started is None or now.timestamp() - started >= OUTAGE_CONFIRM_SECONDS:
                record_event(db, tenant, "whatsapp_desconectado")
                recorded += 1

    ending = (
        db.query(Subscription)
        .filter(
            Subscription.status == SubscriptionStatus.TRIAL.value,
            Subscription.trial_ends_at > now,
            Subscription.trial_ends_at <= now + TRIAL_ENDING_NOTICE,
        )
        .all()
    )
    if ending:
        notified = {
            tenant_id
            for (tenant_id,) in db.query(IntegrationEvent.tenant_id).filter(
                IntegrationEvent.event == "prueba_por_vencer",
                IntegrationEvent.tenant_id.in_([s.tenant_id for s in ending]),
            )
        }
        for sub in ending:
            tenant = db.get(Tenant, sub.tenant_id)
            if tenant is not None and tenant.is_active and sub.tenant_id not in notified:
                record_event(db, tenant, "prueba_por_vencer")
                recorded += 1
    db.commit()
    return recorded


def sign(body: bytes, timestamp: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post(payload: dict[str, Any]) -> Optional[str]:
    """None si siochat respondió 2xx; si no, el motivo para reintentar."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Omitel-Timestamp": timestamp,
        "X-Omitel-Signature": sign(body, timestamp, settings.omitel_webhook_secret.strip()),
    }
    try:
        res = httpx.post(
            settings.integration_webhook_url.strip(), content=body, headers=headers, timeout=SEND_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}"
    return None if 200 <= res.status_code < 300 else f"HTTP {res.status_code}"


def deliver_pending_events(db: Session, now: Optional[datetime] = None) -> dict[str, int]:
    if not integration_enabled():
        return {"sent": 0, "failed": 0}
    now = now or datetime.now(timezone.utc)
    rows = (
        db.query(IntegrationEvent)
        .filter(
            IntegrationEvent.delivered_at.is_(None),
            IntegrationEvent.next_attempt_at <= now,
            IntegrationEvent.attempts <= len(RETRY_DELAYS),
        )
        .order_by(IntegrationEvent.created_at)
        .limit(SENDER_BATCH)
        .with_for_update(skip_locked=True)
        .all()
    )
    sent = failed = 0
    for row in rows:
        error = _post(row.payload)
        if error is None:
            row.delivered_at = now
            row.last_error = None
            sent += 1
            continue
        failed += 1
        row.last_error = error[:300]
        if row.attempts < len(RETRY_DELAYS):
            row.next_attempt_at = now + RETRY_DELAYS[row.attempts]
        row.attempts += 1
        if row.attempts > len(RETRY_DELAYS):
            log.warning("siochat no recibió el evento %s (%s) tras %s intentos", row.id, row.event, row.attempts)
    db.commit()
    return {"sent": sent, "failed": failed}


_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _loop() -> None:
    while not _stop.is_set():
        try:
            with SessionLocal() as db:
                sweep_state_events(db)
                result = deliver_pending_events(db)
            if result["failed"]:
                log.info("Eventos a siochat: %s", result)
        except Exception:
            log.exception("Error enviando eventos a siochat")
        _stop.wait(SENDER_INTERVAL_SECONDS)


def start_event_sender() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="siochat-events", daemon=True)
    _thread.start()


def stop_event_sender() -> None:
    _stop.set()

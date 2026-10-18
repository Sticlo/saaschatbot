"""Que ningún WhatsApp caído pase desapercibido: avisa al dueño y detecta caídas que no llegaron por webhook."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import Subscription, Tenant, User, UserRole, WhatsAppSession
from app.domain.entities.enums import WhatsAppStatus
from app.infrastructure.cache.redis_client import get_redis, tenant_cache_key

log = logging.getLogger(__name__)

# Las caídas de red se recuperan solas en uno o dos minutos: solo se avisa si sigue caído.
OWNER_ALERT_AFTER_SECONDS = 10 * 60
OWNER_REMINDER_AFTER_SECONDS = 24 * 3600
DOWN_MARK_TTL_SECONDS = 14 * 24 * 3600
SILENT_CHECK_INTERVAL_SECONDS = 5 * 60
# Dos lecturas seguidas en «close» antes de marcarlo caído: una sola puede ser un parpadeo.
SILENT_CLOSE_STRIKES = 2

DOWN_STATUSES = (
    WhatsAppStatus.DISCONNECTED.value,
    WhatsAppStatus.CONNECTING.value,
    WhatsAppStatus.BANNED.value,
    WhatsAppStatus.RESTRICTED.value,
)


def _down_key(tenant_id: Any) -> str:
    return tenant_cache_key(str(tenant_id), "wa_down_since")


def clear_down_mark(tenant_id: Any) -> None:
    try:
        get_redis().delete(_down_key(tenant_id), tenant_cache_key(str(tenant_id), "wa_close_strikes"))
    except Exception:
        log.debug("No se pudo limpiar la marca de caída tenant=%s", tenant_id, exc_info=True)


def down_since(tenant_id: Any, now: Optional[float] = None) -> float:
    """Desde cuándo está caído; la primera vez que se ve caído queda marcado ahora."""
    now = now if now is not None else time.time()
    client = get_redis()
    client.set(_down_key(tenant_id), str(now), nx=True, ex=DOWN_MARK_TTL_SECONDS)
    raw = client.get(_down_key(tenant_id))
    try:
        return float(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return now


def _is_live(db: Session, tenant: Tenant) -> bool:
    from app.application.billing.subscription_service import subscription_is_paid, trial_is_running

    sub = db.query(Subscription).filter(Subscription.tenant_id == tenant.id).first()
    return subscription_is_paid(sub) or trial_is_running(sub)


def _owner_email(db: Session, tenant_id: uuid.UUID) -> Optional[str]:
    owner = (
        db.query(User)
        .filter(User.tenant_id == tenant_id, User.role == UserRole.OWNER.value, User.is_active.is_(True))
        .order_by(User.created_at.asc())
        .first()
    )
    return owner.email if owner else None


def _hours_text(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minutos"
    hours = minutes // 60
    return "1 hora" if hours == 1 else f"{hours} horas"


def _email_owner(db: Session, tenant: Tenant, *, elapsed: float, reminder: bool) -> bool:
    from app.infrastructure.email.email_service import send_billing_email

    email = _owner_email(db, tenant.id)
    if not email:
        return False
    blocked = tenant.whatsapp_status in (WhatsAppStatus.BANNED.value, WhatsAppStatus.RESTRICTED.value)
    if blocked:
        subject = "WhatsApp restringió el número de tu negocio"
        title = "WhatsApp restringió tu número"
        paragraphs = [
            f"El WhatsApp de <b>{tenant.business_name}</b> quedó restringido por WhatsApp y la IA no puede responder.",
            "Respóndenos este correo y lo revisamos contigo para cuidar el número.",
        ]
    else:
        subject = ("Sigue desconectado: " if reminder else "") + "Tu WhatsApp se desconectó de Omitel"
        title = "Tu WhatsApp se desconectó"
        paragraphs = [
            f"Hace {_hours_text(elapsed)} el WhatsApp de <b>{tenant.business_name}</b> no está conectado, "
            "así que la IA no está respondiendo a tus clientes.",
            "Para volver a conectarlo: entra al panel, toca <b>Conectar WhatsApp</b> y escanea el código "
            "desde el celular del negocio (WhatsApp › Dispositivos vinculados). Tus chats y tu configuración siguen ahí.",
            "Para que no se vuelva a caer: abre WhatsApp en ese celular al menos una vez cada 14 días, "
            "mantenlo actualizado y no uses «Cerrar todas las sesiones».",
        ]
    try:
        send_billing_email(
            to_email=email,
            subject=subject,
            title=title,
            paragraphs=paragraphs,
            cta_label="Reconectar mi WhatsApp",
            cta_url=f"{settings.app_public_url.rstrip('/')}/panel",
            footer="Omitel · Aviso sobre la conexión de tu WhatsApp.",
        )
    except Exception:
        log.exception("No se pudo avisar la caída de WhatsApp tenant=%s", tenant.id)
        return False
    return True


def notify_owner_if_still_down(db: Session, tenant: Tenant, now: Optional[float] = None) -> Optional[str]:
    """Un correo a los 10 minutos de caído y un recordatorio al día; nada si se recupera antes."""
    from app.application.platform.incidents import record_incident

    if tenant.whatsapp_status not in DOWN_STATUSES or not _is_live(db, tenant):
        return None
    now = now if now is not None else time.time()
    since = down_since(tenant.id, now)
    elapsed = now - since
    if elapsed < OWNER_ALERT_AFTER_SECONDS:
        return None
    kind = "reminder" if elapsed >= OWNER_REMINDER_AFTER_SECONDS else "first"
    sent_key = tenant_cache_key(str(tenant.id), f"wa_down_mail:{int(since)}:{kind}")
    if not get_redis().set(sent_key, "1", nx=True, ex=DOWN_MARK_TTL_SECONDS):
        return None
    if not _email_owner(db, tenant, elapsed=elapsed, reminder=kind == "reminder"):
        get_redis().delete(sent_key)
        return None
    record_incident(
        tenant.id,
        "system.whatsapp_owner_notified",
        "Le avisamos por correo que su WhatsApp sigue desconectado"
        + (" (recordatorio)" if kind == "reminder" else ""),
        details={"kind": kind, "down_minutes": int(elapsed // 60)},
        throttle_seconds=0,
    )
    return kind


def _gateway_says_closed(session: WhatsAppSession) -> Optional[bool]:
    from app.application.whatsapp.whatsapp_gateway import WhatsAppGatewayError, connection_state

    try:
        payload = connection_state(session.instance_name)
    except WhatsAppGatewayError:
        return None
    instance = payload.get("instance") if isinstance(payload.get("instance"), dict) else {}
    state = str(instance.get("state") or payload.get("state") or "").lower()
    if not state:
        return None
    return state == "close"


def detect_silent_drops(db: Session, *, force: bool = False) -> int:
    """Conectados en la base pero cerrados en el gateway (el webhook de la caída nunca llegó)."""
    from app.application.platform.incidents import record_incident
    from app.application.whatsapp.whatsapp_status import apply_session_status

    client = get_redis()
    # Varios workers corren este barrido: solo uno revisa por ventana.
    if not force and not client.set("wa:silent_check_lock", "1", nx=True, ex=SILENT_CHECK_INTERVAL_SECONDS - 10):
        return 0
    rows = (
        db.query(Tenant, WhatsAppSession)
        .join(WhatsAppSession, WhatsAppSession.tenant_id == Tenant.id)
        .filter(Tenant.is_active.is_(True), Tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value)
        .all()
    )
    marked = 0
    unreachable = 0
    for tenant, session in rows:
        closed = _gateway_says_closed(session)
        strikes_key = tenant_cache_key(str(tenant.id), "wa_close_strikes")
        if closed is None:
            unreachable += 1
            # Gateway caído: eso lo avisa el watchdog de salud, no tiene sentido esperar un timeout por negocio.
            if unreachable >= 2:
                break
            continue
        unreachable = 0
        if not closed:
            if closed is False:
                client.delete(strikes_key)
            continue
        strikes = client.incr(strikes_key)
        client.expire(strikes_key, SILENT_CHECK_INTERVAL_SECONDS * 3)
        if strikes < SILENT_CLOSE_STRIKES:
            continue
        client.delete(strikes_key)
        apply_session_status(session, tenant, WhatsAppStatus.DISCONNECTED.value)
        db.commit()
        record_incident(
            tenant.id,
            "system.whatsapp_disconnected",
            "WhatsApp se desconectó sin avisar (lo detectó la revisión periódica)",
            details={"reason": "silent", "code": None, "status": WhatsAppStatus.DISCONNECTED.value},
            throttle_seconds=1800,
            throttle_scope="silent",
        )
        log.warning("Caída silenciosa de WhatsApp detectada tenant=%s", tenant.id)
        marked += 1
    return marked


def gateway_is_reachable() -> bool:
    from app.application.whatsapp.whatsapp_gateway import WhatsAppGatewayError, gateway_request, uses_waha

    if uses_waha():
        return True
    try:
        gateway_request("GET", "/", timeout=5)
        return True
    except WhatsAppGatewayError:
        return False
    except Exception:
        return False

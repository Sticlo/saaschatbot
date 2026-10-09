"""Resumen mensual de resultados para el dueño: un correo y un WhatsApp a su número personal
(los mismos de las alertas con «Todas las alertas»). Nunca se le escribe a los clientes."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.application.results.results_service import (
    compute_results,
    month_key,
    month_name,
    previous_month_period,
    results_paragraphs,
    results_whatsapp_text,
)
from app.config import settings
from app.domain.entities import Tenant, TenantProfile
from app.domain.entities.enums import UserRole, WhatsAppStatus

log = logging.getLogger(__name__)

# Si el servidor estuvo caído el día 1, el resumen sale igual en los días siguientes.
SEND_UNTIL_DAY = 5


def _panel_url() -> str:
    return f"{settings.app_public_url.rstrip('/')}/panel"


def _owner_email(db: Session, tenant_id) -> Optional[str]:
    from app.domain.entities import User

    owner = (
        db.query(User)
        .filter(User.tenant_id == tenant_id, User.role == UserRole.OWNER.value, User.is_active.is_(True))
        .order_by(User.created_at.asc())
        .first()
    )
    return owner.email if owner else None


def _claim(db: Session, profile_id, key: str) -> bool:
    """Con varios workers barriendo, solo uno envía el resumen del mes."""
    claimed = (
        db.query(TenantProfile)
        .filter(
            TenantProfile.id == profile_id,
            or_(TenantProfile.results_report_sent_for.is_(None), TenantProfile.results_report_sent_for != key),
        )
        .update({TenantProfile.results_report_sent_for: key}, synchronize_session=False)
    )
    db.commit()
    return bool(claimed)


def send_tenant_report(db: Session, tenant: Tenant, *, now: datetime) -> bool:
    """True si había algo que contar y se envió por al menos un canal."""
    from app.application.conversations.interest_alert_service import alert_phones, send_alerts
    from app.infrastructure.email.email_service import send_billing_email

    start, end = previous_month_period(now)
    summary = compute_results(db, tenant, start=start, end=end)
    if not summary.has_activity:
        return False
    month = month_name(start)
    period = f"En {month}"
    sent = False

    email = _owner_email(db, tenant.id)
    if email:
        paragraphs = results_paragraphs(summary, period=period)
        if not summary.avg_ticket_cop and summary.ai_appointments:
            paragraphs.append(
                "Pon en tu panel cuánto vale en promedio un servicio y te mostramos cuánta plata te generó."
            )
        try:
            send_billing_email(
                to_email=email,
                subject=f"Lo que Omitel hizo por {tenant.business_name} en {month}",
                title=f"Tus resultados de {month}",
                paragraphs=paragraphs,
                cta_label="Ver mi panel",
                cta_url=_panel_url(),
                footer="Omitel · Resumen mensual de lo que hizo tu asistente.",
            )
            sent = True
        except Exception:
            log.exception("No se pudo enviar el resumen mensual por correo tenant=%s", tenant.id)

    phones = alert_phones(tenant.profile)
    if phones and tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value and tenant.whatsapp_session:
        text = results_whatsapp_text(tenant.business_name, summary, period=period, panel_url=_panel_url())
        sent = send_alerts(tenant.whatsapp_session, phones, text) > 0 or sent
    return sent


def send_monthly_reports(db: Session, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    start, end = previous_month_period(now)
    if now.astimezone(start.tzinfo).day > SEND_UNTIL_DAY:
        return 0
    key = month_key(start)
    pending = (
        db.query(Tenant.id, TenantProfile.id)
        .join(TenantProfile, TenantProfile.tenant_id == Tenant.id)
        .filter(
            Tenant.is_active.is_(True),
            Tenant.created_at < end,
            or_(TenantProfile.results_report_sent_for.is_(None), TenantProfile.results_report_sent_for != key),
        )
        .all()
    )
    sent = 0
    for tenant_id, profile_id in pending:
        if not _claim(db, profile_id, key):
            continue
        tenant = db.get(Tenant, tenant_id)
        try:
            if tenant is not None and send_tenant_report(db, tenant, now=now):
                sent += 1
        except Exception:
            db.rollback()
            log.exception("Error enviando el resumen mensual tenant=%s", tenant_id)
    return sent

"""Resumen semanal por correo para el dueño: qué hizo la IA, qué no supo responder y qué le falta al
inventario. Sale el lunes por la mañana (hora de Colombia), una sola vez por semana."""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from html import escape
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.application.appointments.appointment_service import BOGOTA
from app.application.results.results_service import compute_results, results_paragraphs
from app.config import settings
from app.domain.entities import AiQuestion, Product, Subscription, Tenant, User
from app.domain.entities.enums import UserRole
from app.infrastructure.cache.redis_client import get_redis, tenant_cache_key

log = logging.getLogger(__name__)

SEND_FROM_HOUR = 8
# Si el servidor estuvo caído el lunes, sale igual el martes.
SEND_UNTIL_WEEKDAY = 1
CLAIM_TTL_SECONDS = 9 * 24 * 3600
TOP_QUESTIONS = 5


def _panel_url() -> str:
    return f"{settings.app_public_url.rstrip('/')}/panel"


def _week_period(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(BOGOTA)
    monday = datetime.combine(local.date() - timedelta(days=local.weekday()), time(0, 0), tzinfo=BOGOTA)
    return monday - timedelta(days=7), monday


def _week_key(start: datetime) -> str:
    year, week, _ = start.isocalendar()
    return f"{year}-W{week:02d}"


def _owner_email(db: Session, tenant_id) -> Optional[str]:
    owner = (
        db.query(User)
        .filter(User.tenant_id == tenant_id, User.role == UserRole.OWNER.value, User.is_active.is_(True))
        .order_by(User.created_at.asc())
        .first()
    )
    return owner.email if owner else None


def _is_live(db: Session, tenant_id) -> bool:
    from app.application.billing.subscription_service import subscription_is_paid, trial_is_running

    sub = db.query(Subscription).filter(Subscription.tenant_id == tenant_id).first()
    return subscription_is_paid(sub) or trial_is_running(sub)


def _claim(tenant_id, key: str) -> bool:
    """Con varios workers barriendo, solo uno envía el resumen de la semana."""
    return bool(
        get_redis().set(tenant_cache_key(str(tenant_id), f"weekly_report:{key}"), "1", nx=True, ex=CLAIM_TTL_SECONDS)
    )


def weekly_paragraphs(db: Session, tenant: Tenant, *, start: datetime, end: datetime) -> list[str]:
    """Vacío si no hay nada que contar."""
    paragraphs = results_paragraphs(compute_results(db, tenant, start=start, end=end), period="Esta semana")

    open_questions = (
        db.query(AiQuestion)
        .filter(AiQuestion.tenant_id == tenant.id, AiQuestion.status == "open")
        .order_by(AiQuestion.times_asked.desc(), AiQuestion.last_asked_at.desc())
        .all()
    )
    if open_questions:
        top = "".join(
            f"<br>• {escape(q.topic)}{f' (te lo preguntaron {q.times_asked} veces)' if q.times_asked > 1 else ''}"
            for q in open_questions[:TOP_QUESTIONS]
        )
        count = len(open_questions)
        paragraphs.append(
            f"<strong>La IA no supo responder {count} pregunta{'s' if count != 1 else ''}</strong>. "
            f"Enséñaselas una sola vez en Personalizar IA → Preguntas pendientes y no vuelve a pasar:{top}"
        )

    total, unpriced = (
        db.query(func.count(Product.id), func.count(Product.id).filter(Product.price_cop.is_(None)))
        .filter(Product.tenant_id == tenant.id)
        .one()
    )
    if unpriced:
        paragraphs.append(
            f"{unpriced} de tus {total} productos no tienen precio: cuando un cliente pregunta por ellos, la IA "
            "no puede cotizar. Complétalos en Inventario."
        )
    elif not total and paragraphs:
        paragraphs.append(
            "Todavía no tienes productos en el Inventario. Sube tu Excel, Word o PDF con la lista de precios "
            "y la IA podrá cotizar todo lo que vendes."
        )
    return paragraphs


def send_weekly_reports(db: Session, now: Optional[datetime] = None) -> int:
    from app.infrastructure.email.email_service import send_billing_email

    now = now or datetime.now(timezone.utc)
    local = now.astimezone(BOGOTA)
    if local.weekday() > SEND_UNTIL_WEEKDAY or (local.weekday() == 0 and local.hour < SEND_FROM_HOUR):
        return 0
    start, end = _week_period(now)
    key = _week_key(start)
    tenant_ids = [
        tid for (tid,) in db.query(Tenant.id).filter(Tenant.is_active.is_(True), Tenant.created_at < end).all()
    ]
    sent = 0
    for tenant_id in tenant_ids:
        try:
            if not _is_live(db, tenant_id):
                continue
            email = _owner_email(db, tenant_id)
            if not email:
                continue
            tenant = db.get(Tenant, tenant_id)
            paragraphs = weekly_paragraphs(db, tenant, start=start, end=end)
            if not paragraphs or not _claim(tenant_id, key):
                continue
            send_billing_email(
                to_email=email,
                subject=f"Tu semana con Omitel · {tenant.business_name}",
                title="Así le fue a tu asistente esta semana",
                paragraphs=paragraphs,
                cta_label="Abrir mi panel",
                cta_url=_panel_url(),
                footer="Omitel · Resumen semanal de tu asistente.",
            )
            sent += 1
        except Exception:
            db.rollback()
            log.exception("Error enviando el resumen semanal tenant=%s", tenant_id)
    return sent

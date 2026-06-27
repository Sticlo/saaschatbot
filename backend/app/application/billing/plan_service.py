from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import DEFAULT_PLAN_ID, PREMIUM_PLAN_ID, Plan

PRO_FEATURES = {
    "ai_on_reply": True,
    "realtime_panel": True,
    "maps_scraper": False,
    "priority_support": False,
    "ai_daily_replies": 400,
    "ai_daily_classifications": 0,
}

PREMIUM_FEATURES = {
    "ai_on_reply": True,
    "realtime_panel": True,
    "maps_scraper": True,
    "priority_support": True,
    "ai_daily_replies": 0,
    "ai_daily_classifications": 0,
}


def get_plan_by_slug(db: Session, slug: str) -> Optional[Plan]:
    return (
        db.query(Plan)
        .filter(Plan.slug == slug, Plan.is_active.is_(True))
        .first()
    )


def get_default_plan(db: Session) -> Plan:
    plan = get_plan_by_slug(db, settings.default_plan_slug)
    if plan is None:
        plan = db.query(Plan).filter(Plan.id == DEFAULT_PLAN_ID).first()
    if plan is None:
        raise RuntimeError(
            "Plan por defecto no encontrado. Ejecuta: alembic upgrade head"
        )
    return plan


def list_public_plans(db: Session) -> list[Plan]:
    return (
        db.query(Plan)
        .filter(Plan.is_active.is_(True), Plan.is_public.is_(True))
        .order_by(Plan.sort_order.asc(), Plan.price_cop.asc())
        .all()
    )


def ensure_default_plan(db: Session) -> Plan:
    """Idempotente: crea planes Pro y Premium si la tabla está vacía."""
    existing = db.query(Plan).filter(Plan.slug == settings.default_plan_slug).first()
    if existing:
        return existing

    db.add(
        Plan(
            id=DEFAULT_PLAN_ID,
            slug="pro",
            name="Plan Pro",
            description=(
                "IA que saluda, responde dudas y te avisa quién quiere comprar. "
                "Ideal para negocios con WhatsApp activo."
            ),
            price_cop=120_000,
            price_usd_cents=3_000,
            trial_bait_limit=settings.trial_bait_limit,
            daily_bait_limit=50,
            max_team_members=2,
            features=PRO_FEATURES,
            is_active=True,
            is_public=True,
            sort_order=0,
        )
    )
    db.add(
        Plan(
            id=PREMIUM_PLAN_ID,
            slug="premium",
            name="Plan Premium",
            description=(
                "Todo lo del Pro con IA ilimitada, más equipo y soporte prioritario. "
                "Para alto volumen en WhatsApp."
            ),
            price_cop=200_000,
            price_usd_cents=5_000,
            trial_bait_limit=settings.trial_bait_limit,
            daily_bait_limit=100,
            max_team_members=5,
            features=PREMIUM_FEATURES,
            is_active=True,
            is_public=True,
            sort_order=1,
        )
    )
    db.commit()
    plan = db.query(Plan).filter(Plan.id == DEFAULT_PLAN_ID).first()
    assert plan is not None
    return plan

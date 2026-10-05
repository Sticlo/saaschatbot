from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import DEFAULT_PLAN_ID, Plan

PLAN_FEATURES = {
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
    """Idempotente: crea el plan único si la tabla está vacía."""
    existing = db.query(Plan).filter(Plan.slug == settings.default_plan_slug).first()
    if existing:
        return existing

    db.add(
        Plan(
            id=DEFAULT_PLAN_ID,
            slug="pro",
            name="Plan Pro",
            description=(
                "Todo Omitel incluido: IA sin límite que atiende tu WhatsApp, catálogo, citas, "
                "equipo de hasta 5 personas y soporte prioritario."
            ),
            price_cop=200_000,
            price_usd_cents=5_000,
            trial_bait_limit=settings.trial_bait_limit,
            daily_bait_limit=100,
            max_team_members=5,
            features=PLAN_FEATURES,
            is_active=True,
            is_public=True,
            sort_order=0,
        )
    )
    db.commit()
    plan = db.query(Plan).filter(Plan.id == DEFAULT_PLAN_ID).first()
    assert plan is not None
    return plan

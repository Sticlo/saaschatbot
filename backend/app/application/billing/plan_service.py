from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import DEFAULT_PLAN_ID, Plan


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
    """Idempotente: crea el plan Pro si la tabla existe pero está vacía."""
    existing = db.query(Plan).filter(Plan.slug == settings.default_plan_slug).first()
    if existing:
        return existing

    plan = Plan(
        id=DEFAULT_PLAN_ID,
        slug=settings.default_plan_slug,
        name="Plan Pro",
        description=(
            "Prospecta por WhatsApp con IA, extractor de Google Maps "
            "y panel en tiempo real."
        ),
        price_cop=80_000,
        price_usd_cents=2_000,
        trial_bait_limit=settings.trial_bait_limit,
        daily_bait_limit=settings.paid_daily_bait_limit,
        max_team_members=3,
        features={
            "ai_on_reply": True,
            "maps_scraper": True,
            "realtime_panel": True,
        },
        is_active=True,
        is_public=True,
        sort_order=0,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan

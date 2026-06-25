from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.infrastructure.persistence.database import get_db
from app.presentation.schemas.plans import PlanResponse
from app.application.billing.plan_service import list_public_plans

router = APIRouter(prefix="/plans", tags=["plans"])


@router.get("", response_model=list[PlanResponse])
def list_plans(db: Session = Depends(get_db)):
    """Catálogo público de planes disponibles para compra."""
    return list_public_plans(db)

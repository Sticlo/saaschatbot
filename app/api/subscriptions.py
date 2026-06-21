from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import RequireViewer
from app.database import get_db
from app.models import Tenant
from app.schemas.plans import SubscriptionSummaryResponse
from app.services.subscription_service import (
    build_subscription_summary,
    get_tenant_subscription,
)

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.get("/me", response_model=SubscriptionSummaryResponse)
def get_my_subscription(current: RequireViewer, db: Session = Depends(get_db)):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    subscription = get_tenant_subscription(db, current.tenant_id)
    if subscription is None or subscription.plan is None:
        raise HTTPException(status_code=404, detail="Suscripción no encontrada")

    return build_subscription_summary(tenant, subscription, subscription.plan)

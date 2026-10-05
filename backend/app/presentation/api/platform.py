"""Consola del dueño de la plataforma: todas las empresas, su historial y lo que se les permite."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.application.platform.platform_service import (
    HISTORY_KINDS,
    PlatformError,
    list_accounts,
    list_tenants,
    tenant_detail,
    tenant_history,
    update_tenant,
)
from app.domain.entities import Plan, Tenant
from app.infrastructure.persistence.database import get_db
from app.shared.core.deps import RequirePlatformAdmin
from app.shared.core.rate_limit import client_ip

router = APIRouter(prefix="/platform", tags=["platform"])


class PlatformTenantUpdate(BaseModel):
    plan_id: Optional[uuid.UUID] = None
    is_active: Optional[bool] = None
    grant_paid_days: Optional[int] = Field(default=None, ge=1, le=365)
    grant_unlimited: bool = False
    extend_trial_days: Optional[int] = Field(default=None, ge=1, le=60)
    overrides: Optional[dict[str, Any]] = None
    reset_ai_today: bool = False


def _tenant_or_404(db: Session, tenant_id: uuid.UUID) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    return tenant


@router.get("/me")
def platform_me(current: RequirePlatformAdmin):
    return {"email": current.email}


@router.get("/plans")
def platform_plans(current: RequirePlatformAdmin, db: Session = Depends(get_db)):
    plans = db.query(Plan).filter(Plan.is_active.is_(True)).order_by(Plan.sort_order.asc()).all()
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "price_cop": p.price_cop,
            "max_team_members": p.max_team_members,
            "features": p.features or {},
        }
        for p in plans
    ]


@router.get("/tenants")
def platform_tenants(
    current: RequirePlatformAdmin,
    db: Session = Depends(get_db),
    q: str = Query(default="", max_length=120),
):
    return list_tenants(db, query=q)


@router.get("/accounts")
def platform_accounts(
    current: RequirePlatformAdmin,
    db: Session = Depends(get_db),
    q: str = Query(default="", max_length=120),
):
    return list_accounts(db, query=q)


@router.get("/tenants/{tenant_id}")
def platform_tenant(tenant_id: uuid.UUID, current: RequirePlatformAdmin, db: Session = Depends(get_db)):
    return tenant_detail(db, _tenant_or_404(db, tenant_id))


@router.get("/tenants/{tenant_id}/history")
def platform_tenant_history(
    tenant_id: uuid.UUID,
    current: RequirePlatformAdmin,
    db: Session = Depends(get_db),
    kind: str = Query(default="all"),
    limit: int = Query(default=100, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
):
    if kind not in HISTORY_KINDS:
        raise HTTPException(status_code=400, detail="Filtro inválido")
    _tenant_or_404(db, tenant_id)
    return tenant_history(db, tenant_id, kind=kind, limit=limit, offset=offset)


@router.patch("/tenants/{tenant_id}")
def platform_update_tenant(
    tenant_id: uuid.UUID,
    body: PlatformTenantUpdate,
    request: Request,
    current: RequirePlatformAdmin,
    db: Session = Depends(get_db),
):
    tenant = _tenant_or_404(db, tenant_id)
    try:
        changed = update_tenant(
            db,
            tenant,
            admin_email=current.email,
            admin_user_id=current.id,
            admin_tenant_id=current.tenant_id,
            ip_address=client_ip(request),
            plan_id=body.plan_id,
            is_active=body.is_active,
            grant_paid_days=body.grant_paid_days,
            grant_unlimited=body.grant_unlimited,
            extend_trial_days=body.extend_trial_days,
            overrides=body.overrides,
            reset_ai_today=body.reset_ai_today,
        )
    except PlatformError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.refresh(tenant)
    return {"changed": changed, "tenant": tenant_detail(db, tenant)}

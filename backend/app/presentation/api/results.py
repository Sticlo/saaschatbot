from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.application.billing.tenant_service import log_audit
from app.application.results.results_service import normalize_ticket, tenant_results
from app.domain.entities import Tenant
from app.infrastructure.persistence.database import get_db
from app.shared.core.deps import RequireOwner, RequireViewer

router = APIRouter(prefix="/results", tags=["results"])


class ResultsSettingsUpdate(BaseModel):
    avg_ticket_cop: Optional[int] = Field(default=None, ge=0)


def _payload(db: Session, tenant_id) -> dict[str, Any]:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    get_or_create_tenant_profile(db, tenant_id)
    db.commit()
    db.refresh(tenant)
    return tenant_results(db, tenant)


@router.get("")
def get_results(current: RequireViewer, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _payload(db, current.tenant_id)


@router.put("/settings")
def put_results_settings(
    body: ResultsSettingsUpdate,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        ticket = normalize_ticket(body.avg_ticket_cop)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    profile = get_or_create_tenant_profile(db, current.tenant_id)
    profile.avg_ticket_cop = ticket
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="results.settings_updated",
        details={"avg_ticket_cop": ticket},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return _payload(db, current.tenant_id)

from __future__ import annotations

import uuid

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.shared.core.deps import RequireAgent, RequireOwner, RequireViewer
from app.infrastructure.persistence.database import get_db
from app.domain.entities import Campaign, Lead, Tenant
from app.application.outbound.bait_limit_service import build_limits_summary
from app.application.outbound.bait_scheduler import is_outbound_paused, set_outbound_paused
from app.application.outbound.outbound_service import enqueue_campaign, get_queue_stats, import_leads
from app.application.billing.tenant_service import log_audit
from app.presentation.schemas.outbound import (
    CampaignResponse,
    EnqueueCampaignRequest,
    EnqueueCampaignResponse,
    ImportLeadsRequest,
    ImportLeadsResponse,
    LeadResponse,
    OutboundLimitsResponse,
    PauseOutboundRequest,
    QueueStatsResponse,
)

router = APIRouter(prefix="/outbound", tags=["outbound"])


@router.get("/limits", response_model=OutboundLimitsResponse)
def outbound_limits(current: RequireViewer, db: Session = Depends(get_db)):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    return build_limits_summary(db, tenant)


@router.get("/queue/stats", response_model=QueueStatsResponse)
def queue_stats(current: RequireViewer, db: Session = Depends(get_db)):
    stats = get_queue_stats(db, current.tenant_id)
    stats["outbound_paused"] = is_outbound_paused(current.tenant_id)
    return stats


@router.get("/leads", response_model=list[LeadResponse])
def list_leads(
    current: RequireViewer,
    db: Session = Depends(get_db),
    status: Optional[str] = None,
    limit: int = 100,
):
    query = db.query(Lead).filter(Lead.tenant_id == current.tenant_id)
    if status:
        query = query.filter(Lead.status == status)
    rows = query.order_by(Lead.created_at.desc()).limit(min(limit, 500)).all()
    return rows


@router.post("/leads", response_model=ImportLeadsResponse)
def create_leads(
    body: ImportLeadsRequest,
    request: Request,
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    items = [{"phone": l.phone, "name": l.name} for l in body.leads]
    result = import_leads(db, tenant_id=tenant.id, items=items, source=body.source)
    log_audit(
        db,
        tenant_id=tenant.id,
        user_id=current.id,
        action="outbound.leads_imported",
        details=result,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return result


@router.post("/campaigns/enqueue", response_model=EnqueueCampaignResponse)
def start_campaign(
    body: EnqueueCampaignRequest,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    if not tenant.disclaimer_accepted_at:
        raise HTTPException(
            status_code=403,
            detail="Acepta el disclaimer en configuración antes de enviar carnadas",
        )

    result = enqueue_campaign(
        db,
        tenant=tenant,
        lead_ids=body.lead_ids,
        limit=body.limit,
        campaign_name=body.name,
        message_template=body.message_template,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "No se pudo encolar"))

    log_audit(
        db,
        tenant_id=tenant.id,
        user_id=current.id,
        action="outbound.campaign_started",
        details=result,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return result


@router.get("/campaigns", response_model=list[CampaignResponse])
def list_campaigns(current: RequireViewer, db: Session = Depends(get_db), limit: int = 20):
    rows = (
        db.query(Campaign)
        .filter(Campaign.tenant_id == current.tenant_id)
        .order_by(Campaign.created_at.desc())
        .limit(min(limit, 50))
        .all()
    )
    return rows


@router.post("/pause")
def pause_outbound(
    body: PauseOutboundRequest,
    current: RequireOwner,
):
    set_outbound_paused(current.tenant_id, body.paused)
    return {"paused": body.paused}

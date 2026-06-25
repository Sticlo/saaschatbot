from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.shared.core.deps import RequireOwner, RequireViewer
from app.infrastructure.persistence.database import get_db
from app.domain.entities import Tenant, TenantProfile
from app.infrastructure.cache.redis_client import cache_delete, tenant_cache_key
from app.presentation.schemas.auth import (
    TenantProfileResponse,
    TenantProfileUpdate,
    TenantResponse,
    TenantSettingsUpdate,
)
from app.application.realtime.realtime_service import publish_tenant_settings
from app.application.billing.tenant_service import log_audit

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("/me", response_model=TenantResponse)
def get_my_tenant(current: RequireViewer, db: Session = Depends(get_db)):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    return tenant


@router.patch("/me", response_model=TenantResponse)
def update_my_tenant(
    body: TenantSettingsUpdate,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    if body.business_name is not None:
        tenant.business_name = body.business_name.strip()
    if body.ai_global_enabled is not None:
        tenant.ai_global_enabled = body.ai_global_enabled
        cache_delete(tenant_cache_key(str(tenant.id), "ai_global"))

    log_audit(
        db,
        tenant_id=tenant.id,
        user_id=current.id,
        action="tenant.updated",
        details=body.model_dump(exclude_none=True),
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(tenant)
    publish_tenant_settings(tenant)
    if body.ai_global_enabled:
        from app.domain.entities import Conversation
        from app.application.ai.ai_service import maybe_schedule_ai_for_conversation

        rows = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant.id,
                Conversation.ai_active.is_(True),
            )
            .all()
        )
        for row in rows:
            maybe_schedule_ai_for_conversation(
                db,
                tenant_id=tenant.id,
                conversation_id=row.id,
            )
    return tenant


@router.get("/me/profile", response_model=TenantProfileResponse)
def get_profile(current: RequireViewer, db: Session = Depends(get_db)):
    profile = (
        db.query(TenantProfile)
        .filter(TenantProfile.tenant_id == current.tenant_id)
        .first()
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    return profile


@router.put("/me/profile", response_model=TenantProfileResponse)
def update_profile(
    body: TenantProfileUpdate,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    profile = (
        db.query(TenantProfile)
        .filter(TenantProfile.tenant_id == current.tenant_id)
        .first()
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")

    if body.onboarding_answers is not None:
        profile.onboarding_answers = body.onboarding_answers
    if body.ai_system_prompt is not None:
        profile.ai_system_prompt = body.ai_system_prompt
    if body.bait_message_template is not None:
        profile.bait_message_template = body.bait_message_template

    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="tenant.profile_updated",
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/me/accept-disclaimer", response_model=TenantResponse)
def accept_disclaimer(
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    tenant.disclaimer_accepted_at = datetime.now(timezone.utc)
    log_audit(
        db,
        tenant_id=tenant.id,
        user_id=current.id,
        action="tenant.disclaimer_accepted",
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(tenant)
    return tenant

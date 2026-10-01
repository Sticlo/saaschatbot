from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.shared.core.deps import RequireOwner, RequireViewer
from app.infrastructure.persistence.database import get_db
from app.domain.entities import Tenant, TenantProfile
from app.infrastructure.cache.redis_client import cache_delete, tenant_cache_key
from app.presentation.schemas.auth import (
    InterestAlertResponse,
    InterestAlertSettings,
    TenantProfileResponse,
    TenantProfileUpdate,
    TenantResponse,
    TenantSettingsUpdate,
)
from app.application.realtime.realtime_service import publish_tenant_settings
from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
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
    profile = get_or_create_tenant_profile(db, current.tenant_id)
    db.commit()
    return profile


@router.put("/me/profile", response_model=TenantProfileResponse)
def update_profile(
    body: TenantProfileUpdate,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    profile = get_or_create_tenant_profile(db, current.tenant_id)

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


def _interest_alert_response(db: Session, profile: TenantProfile) -> InterestAlertResponse:
    from app.application.conversations.interest_alert_service import (
        unanswered_interested_conversations,
    )

    return InterestAlertResponse(
        alert_phone=profile.alert_phone or "",
        alert_threshold=profile.alert_threshold,
        pending_count=len(unanswered_interested_conversations(db, profile.tenant_id)),
    )


@router.get("/me/interest-alert", response_model=InterestAlertResponse)
def get_interest_alert(current: RequireViewer, db: Session = Depends(get_db)):
    profile = get_or_create_tenant_profile(db, current.tenant_id)
    db.commit()
    return _interest_alert_response(db, profile)


@router.put("/me/interest-alert", response_model=InterestAlertResponse)
def update_interest_alert(
    body: InterestAlertSettings,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    from app.application.conversations.interest_alert_service import (
        InterestAlertError,
        normalize_alert_phone,
    )

    try:
        phone = normalize_alert_phone(body.alert_phone)
    except InterestAlertError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    profile = get_or_create_tenant_profile(db, current.tenant_id)
    profile.alert_phone = phone or None
    profile.alert_threshold = body.alert_threshold
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="tenant.interest_alert_updated",
        details={"alert_threshold": body.alert_threshold, "enabled": bool(phone)},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(profile)
    return _interest_alert_response(db, profile)


@router.post("/me/interest-alert/test", status_code=status.HTTP_204_NO_CONTENT)
def test_interest_alert(current: RequireOwner, db: Session = Depends(get_db)):
    from app.application.conversations.interest_alert_service import send_alert
    from app.application.whatsapp.whatsapp_status import can_send_whatsapp

    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    profile = get_or_create_tenant_profile(db, tenant.id)
    if not profile.alert_phone:
        raise HTTPException(status_code=400, detail="Primero guarda el número que recibirá las alertas")
    ok, reason = can_send_whatsapp(tenant.whatsapp_status)
    if not ok or tenant.whatsapp_session is None:
        raise HTTPException(status_code=400, detail=reason or "Conecta tu WhatsApp primero")
    try:
        send_alert(
            tenant.whatsapp_session,
            profile.alert_phone,
            f"✅ Prueba de alertas de *{tenant.business_name}*: aquí te avisaremos cuando "
            f"tengas {profile.alert_threshold} clientes interesados sin responder.",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo enviar la prueba: {exc}")


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

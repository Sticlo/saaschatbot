from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.shared.core.deps import RequireOwner, RequireViewer
from app.infrastructure.persistence.database import get_db
from app.domain.entities import Tenant, TenantProfile, WhatsAppSession
from app.infrastructure.cache.redis_client import cache_delete, tenant_cache_key
from app.presentation.schemas.auth import (
    FollowupSettings,
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
    if body.ai_global_enabled and not tenant.ai_global_enabled:
        from app.application.ai.ai_readiness_service import not_ready_message, tenant_readiness

        readiness = tenant_readiness(db, tenant.id)
        if not readiness["ready"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=not_ready_message(readiness))
    if body.ai_global_enabled is not None:
        if body.ai_global_enabled and not tenant.ai_global_enabled:
            from app.domain.entities import Conversation

            db.query(Conversation).filter(
                Conversation.tenant_id == tenant.id,
                Conversation.ai_override_global.is_(True),
            ).update({Conversation.ai_override_global: False}, synchronize_session=False)
        tenant.ai_global_enabled = body.ai_global_enabled
        tenant.ai_autostart_pending = False
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


def _whatsapp_session(db: Session, tenant_id) -> Optional[WhatsAppSession]:
    return db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()


def _interest_alert_response(db: Session, profile: TenantProfile) -> InterestAlertResponse:
    from app.application.conversations.interest_alert_service import (
        OWN_NUMBER_WARNING,
        alert_recipients,
        own_number_recipients,
        unanswered_interested_conversations,
    )

    recipients = alert_recipients(profile)
    own = own_number_recipients(recipients, _whatsapp_session(db, profile.tenant_id))
    return InterestAlertResponse(
        recipients=recipients,
        alert_threshold=profile.alert_threshold,
        pending_count=len(unanswered_interested_conversations(db, profile.tenant_id)),
        warning=OWN_NUMBER_WARNING if own else None,
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
        OWN_NUMBER_WARNING,
        InterestAlertError,
        normalize_alert_recipients,
        own_number_recipients,
    )

    try:
        recipients = normalize_alert_recipients([r.model_dump() for r in body.recipients])
    except InterestAlertError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if own_number_recipients(recipients, _whatsapp_session(db, current.tenant_id)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=OWN_NUMBER_WARNING)

    profile = get_or_create_tenant_profile(db, current.tenant_id)
    profile.alert_recipients = recipients
    profile.alert_threshold = body.alert_threshold
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="tenant.interest_alert_updated",
        details={"alert_threshold": body.alert_threshold, "recipients": len(recipients)},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(profile)
    return _interest_alert_response(db, profile)


@router.get("/me/followup", response_model=FollowupSettings)
def get_followup(current: RequireViewer, db: Session = Depends(get_db)):
    profile = get_or_create_tenant_profile(db, current.tenant_id)
    db.commit()
    return FollowupSettings(followup_hours=profile.followup_hours)


@router.put("/me/followup", response_model=FollowupSettings)
def update_followup(
    body: FollowupSettings,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    from app.application.ai.ai_followup_service import FOLLOWUP_HOURS_CHOICES

    if body.followup_hours not in FOLLOWUP_HOURS_CHOICES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Elige una de las opciones")
    profile = get_or_create_tenant_profile(db, current.tenant_id)
    profile.followup_hours = body.followup_hours
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="tenant.followup_updated",
        details={"followup_hours": body.followup_hours},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return FollowupSettings(followup_hours=profile.followup_hours)


@router.post("/me/interest-alert/test", status_code=status.HTTP_204_NO_CONTENT)
def test_interest_alert(current: RequireOwner, db: Session = Depends(get_db)):
    from app.application.conversations.interest_alert_service import (
        SCOPE_SALES,
        alert_recipients,
        send_alert,
    )
    from app.application.whatsapp.whatsapp_status import can_send_whatsapp

    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    profile = get_or_create_tenant_profile(db, tenant.id)
    recipients = alert_recipients(profile)
    if not recipients:
        raise HTTPException(status_code=400, detail="Primero guarda los números que recibirán las alertas")
    ok, reason = can_send_whatsapp(tenant.whatsapp_status)
    if not ok or tenant.whatsapp_session is None:
        raise HTTPException(status_code=400, detail=reason or "Conecta tu WhatsApp primero")

    failed: list[str] = []
    for recipient in recipients:
        if recipient.get("scope") == SCOPE_SALES:
            what = "cuando un cliente ya quiera comprar o la IA agende una cita, con sus datos para atenderlo"
        else:
            what = (
                f"cuando haya {profile.alert_threshold} clientes interesados sin responder, "
                "cuando un cliente ya quiera comprar y cuando la IA agende una cita"
            )
        greeting = f"Hola {recipient['name']} 👋 " if recipient.get("name") else ""
        try:
            send_alert(
                tenant.whatsapp_session,
                recipient["phone"],
                f"✅ {greeting}Prueba de alertas de *{tenant.business_name}*: por aquí te avisaremos {what}.",
            )
        except Exception:
            failed.append(recipient.get("name") or recipient["phone"])
    if failed:
        raise HTTPException(status_code=502, detail=f"No se pudo enviar la prueba a: {', '.join(failed)}")


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

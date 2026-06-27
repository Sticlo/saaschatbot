from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.application.ai.ai_bait_service import generate_bait_template
from app.application.ai.ai_business_profile_service import (
    build_ai_system_prompt_from_answers,
    business_summary_lines,
)
from app.application.billing.tenant_service import log_audit
from app.application.outbound.bait_template_service import (
    create_template,
    delete_template,
    get_template,
    list_templates,
    preview_template,
    update_template,
)
from app.application.outbound.tenant_asset_service import save_tenant_image
from app.application.billing.tenant_profile_service import (
    answers_from_profile,
    apply_business_answers,
    get_or_create_tenant_profile,
)
from app.domain.entities import BaitTemplate, Tenant
from app.infrastructure.ai.deepseek_client import DeepSeekError
from app.infrastructure.persistence.database import get_db
from app.presentation.schemas.bait_templates import (
    AssetUploadResponse,
    BaitButtonResponse,
    BaitTemplateCreate,
    BaitTemplatePreviewRequest,
    BaitTemplatePreviewResponse,
    BaitTemplateResponse,
    BaitTemplateUpdate,
    BusinessProfileResponse,
    BusinessProfileUpdate,
    GenerateBaitTemplateRequest,
    GenerateBaitTemplateResponse,
)
from app.shared.core.deps import RequireAgent, RequireOwner, RequireViewer

router = APIRouter(prefix="/outbound", tags=["outbound"])


def _template_response(row: BaitTemplate) -> BaitTemplateResponse:
    buttons = [
        BaitButtonResponse(label=b["label"], value=b.get("value") or b["label"])
        for b in (row.buttons or [])
        if isinstance(b, dict) and b.get("label")
    ]
    image_url = f"/api/v1/outbound/assets/{row.image_path.split('/')[-1]}" if row.image_path else None
    return BaitTemplateResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        text=row.text,
        image_path=row.image_path,
        image_url=image_url,
        buttons=buttons or None,
        button_title=row.button_title,
        button_footer=row.button_footer,
        is_default=row.is_default,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _answers_from_profile(profile) -> dict:
    return answers_from_profile(profile)


def _business_summary(business_name: str, answers: dict) -> list[str]:
    return business_summary_lines(business_name=business_name, answers=answers)


def _profile_response(tenant: Tenant, profile) -> BusinessProfileResponse:
    answers = _answers_from_profile(profile)
    return BusinessProfileResponse(
        business_name=tenant.business_name,
        industry=answers.get("industry"),
        products_services=answers.get("products_services"),
        target_customer=answers.get("target_customer"),
        price_range=answers.get("price_range"),
        location_hours=answers.get("location_hours"),
        tone=answers.get("tone"),
        restrictions=answers.get("restrictions"),
        maps_prospect_business=answers.get("maps_prospect_business"),
        maps_prospect_city=answers.get("maps_prospect_city"),
        ai_system_prompt=profile.ai_system_prompt,
        bait_message_template=profile.bait_message_template,
        ai_summary=_business_summary(tenant.business_name, answers),
    )


@router.get("/business-profile", response_model=BusinessProfileResponse)
def get_business_profile(current: RequireViewer, db: Session = Depends(get_db)):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    profile = get_or_create_tenant_profile(db, tenant.id)
    db.commit()
    return _profile_response(tenant, profile)


@router.put("/business-profile", response_model=BusinessProfileResponse)
def update_business_profile(
    body: BusinessProfileUpdate,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    profile = get_or_create_tenant_profile(db, tenant.id)

    answers = _answers_from_profile(profile)
    for key in (
        "industry",
        "products_services",
        "target_customer",
        "price_range",
        "location_hours",
        "tone",
        "restrictions",
        "maps_prospect_business",
        "maps_prospect_city",
    ):
        value = getattr(body, key)
        if value is not None:
            answers[key] = value.strip()
    apply_business_answers(profile, answers)

    if body.ai_system_prompt is not None:
        profile.ai_system_prompt = body.ai_system_prompt.strip() or None
    else:
        profile.ai_system_prompt = build_ai_system_prompt_from_answers(
            business_name=tenant.business_name,
            answers=answers,
        )
    if body.bait_message_template is not None:
        profile.bait_message_template = body.bait_message_template.strip() or None

    log_audit(
        db,
        tenant_id=tenant.id,
        user_id=current.id,
        action="outbound.business_profile_updated",
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(profile)
    return _profile_response(tenant, profile)


@router.post("/assets", response_model=AssetUploadResponse)
def upload_asset(
    request: Request,
    current: RequireOwner,
    file: UploadFile = File(...),
):
    path = save_tenant_image(current.tenant_id, file)
    return AssetUploadResponse(
        image_path=path,
        image_url=f"/api/v1/outbound/assets/{path.split('/')[-1]}",
    )


@router.get("/assets/{filename}")
def get_asset(filename: str, current: RequireViewer):
    from fastapi.responses import FileResponse

    from app.application.outbound.tenant_asset_service import resolve_asset_path

    try:
        path = resolve_asset_path(
            f"assets/{current.tenant_id}/{filename}",
            tenant_id=current.tenant_id,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path)


@router.get("/templates", response_model=list[BaitTemplateResponse])
def list_bait_templates(current: RequireViewer, db: Session = Depends(get_db)):
    rows = list_templates(db, current.tenant_id)
    return [_template_response(r) for r in rows]


@router.post("/templates", response_model=BaitTemplateResponse)
def create_bait_template(
    body: BaitTemplateCreate,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    if body.image_path and not body.image_path.startswith(f"assets/{current.tenant_id}/"):
        raise HTTPException(status_code=400, detail="Imagen no pertenece a tu cuenta")
    row = create_template(
        db,
        tenant_id=current.tenant_id,
        name=body.name,
        text=body.text,
        image_path=body.image_path,
        buttons=[b.model_dump() for b in body.buttons],
        button_title=body.button_title,
        button_footer=body.button_footer,
        is_default=body.is_default,
    )
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="outbound.template_created",
        details={"template_id": str(row.id)},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(row)
    return _template_response(row)


@router.put("/templates/{template_id}", response_model=BaitTemplateResponse)
def update_bait_template(
    template_id: uuid.UUID,
    body: BaitTemplateUpdate,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    row = get_template(db, current.tenant_id, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    if body.image_path and not body.image_path.startswith(f"assets/{current.tenant_id}/"):
        raise HTTPException(status_code=400, detail="Imagen no pertenece a tu cuenta")
    row = update_template(
        db,
        row,
        name=body.name,
        text=body.text,
        image_path=body.image_path,
        clear_image=body.clear_image,
        buttons=[b.model_dump() for b in body.buttons] if body.buttons is not None else None,
        button_title=body.button_title,
        button_footer=body.button_footer,
        is_default=body.is_default,
    )
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="outbound.template_updated",
        details={"template_id": str(row.id)},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(row)
    return _template_response(row)


@router.delete("/templates/{template_id}")
def remove_bait_template(
    template_id: uuid.UUID,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    row = get_template(db, current.tenant_id, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    delete_template(db, row)
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="outbound.template_deleted",
        details={"template_id": str(template_id)},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return {"ok": True}


@router.post("/templates/{template_id}/preview", response_model=BaitTemplatePreviewResponse)
def preview_bait_template(
    template_id: uuid.UUID,
    body: BaitTemplatePreviewRequest,
    current: RequireViewer,
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    row = get_template(db, current.tenant_id, template_id)
    if tenant is None or row is None:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    data = preview_template(row, tenant=tenant, contact_name=body.contact_name)
    image_url = (
        f"/api/v1/outbound/assets/{data['image_path'].split('/')[-1]}"
        if data.get("image_path")
        else None
    )
    return BaitTemplatePreviewResponse(
        text=data["text"],
        image_path=data.get("image_path"),
        image_url=image_url,
        buttons=[
            BaitButtonResponse(label=b["label"], value=b.get("value") or b["label"])
            for b in data.get("buttons") or []
        ],
        button_title=data.get("button_title"),
        button_footer=data.get("button_footer"),
    )


@router.post("/templates/generate", response_model=GenerateBaitTemplateResponse)
def generate_template_with_ai(
    body: GenerateBaitTemplateRequest,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    profile = get_or_create_tenant_profile(db, tenant.id)
    try:
        data = generate_bait_template(tenant=tenant, profile=profile, tone=body.tone)
    except DeepSeekError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return GenerateBaitTemplateResponse(
        text=data["text"],
        buttons=[
            BaitButtonResponse(label=b["label"], value=b["value"])
            for b in data.get("buttons") or []
        ],
        button_title=data.get("button_title"),
        button_footer=data.get("button_footer"),
    )

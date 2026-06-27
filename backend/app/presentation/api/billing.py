from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.application.billing.checkout_service import (
    CheckoutError,
    create_checkout,
    get_checkout_for_tenant,
    handle_wompi_event,
)
from app.application.billing.wompi_service import verify_event_checksum, wompi_enabled
from app.config import settings
from app.domain.entities import Tenant
from app.infrastructure.persistence.database import get_db
from app.presentation.schemas.billing import (
    BillingConfigResponse,
    CheckoutCreateRequest,
    CheckoutCreateResponse,
    CheckoutStatusResponse,
)
from app.shared.core.deps import RequireOwner

router = APIRouter(prefix="/billing", tags=["billing"])
log = logging.getLogger(__name__)


@router.get("/config", response_model=BillingConfigResponse)
def billing_config():
    enabled = wompi_enabled()
    return BillingConfigResponse(
        enabled=enabled,
        public_key=settings.wompi_public_key if enabled else None,
    )


@router.post("/checkout", response_model=CheckoutCreateResponse)
def start_checkout(
    body: CheckoutCreateRequest,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    try:
        payload = create_checkout(
            db,
            tenant=tenant,
            user=current.user,
            plan_slug=body.plan_slug.strip().lower(),
        )
    except CheckoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    return CheckoutCreateResponse(**payload)


@router.get("/checkout/{reference}", response_model=CheckoutStatusResponse)
def checkout_status(
    reference: str,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    checkout = get_checkout_for_tenant(
        db, tenant_id=current.tenant_id, reference=reference.strip()
    )
    if checkout is None:
        raise HTTPException(status_code=404, detail="Checkout no encontrado")

    plan_slug = None
    plan_name = None
    if checkout.plan is not None:
        plan_slug = checkout.plan.slug
        plan_name = checkout.plan.name

    return CheckoutStatusResponse(
        reference=checkout.reference,
        status=checkout.status,
        plan_slug=plan_slug,
        plan_name=plan_name,
        paid_at=checkout.paid_at,
        wompi_transaction_id=checkout.wompi_transaction_id,
    )


@router.post("/wompi/webhook", status_code=status.HTTP_200_OK)
async def wompi_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_event_checksum: Optional[str] = Header(default=None, alias="X-Event-Checksum"),
):
    try:
        event = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON inválido") from exc

    if not verify_event_checksum(event, x_event_checksum):
        log.warning("Wompi webhook checksum inválido")
        raise HTTPException(status_code=401, detail="Checksum inválido")

    try:
        handle_wompi_event(db, event)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("Error procesando webhook Wompi")
        raise HTTPException(status_code=500, detail="Error interno") from None

    return {"ok": True}

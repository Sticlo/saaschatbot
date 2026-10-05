from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.application.billing.checkout_service import (
    CheckoutError,
    create_checkout,
    get_checkout_by_reference,
    get_checkout_for_tenant,
    handle_wompi_event,
    site_checkout_return_url,
    sync_checkout_by_reference,
    sync_checkout_with_wompi,
)
from app.application.billing.auto_renew_service import (
    AutoRenewError,
    cancel_subscription,
    remove_payment_method,
    resume_subscription,
    save_payment_method,
)
from app.application.billing.subscription_service import get_tenant_subscription
from app.application.billing.wompi_service import (
    WompiError,
    get_acceptance_tokens,
    verify_event_checksum,
    wompi_api_base,
    wompi_enabled,
    wompi_is_sandbox,
    wompi_sync_enabled,
)
from app.config import settings
from app.domain.entities import Tenant
from app.infrastructure.persistence.database import get_db
from app.presentation.schemas.billing import (
    BillingConfigResponse,
    CheckoutCreateRequest,
    CheckoutCreateResponse,
    CancelSubscriptionRequest,
    CheckoutStatusResponse,
    CheckoutSyncRequest,
    PaymentMethodRequest,
    PaymentMethodResponse,
    WompiTermsResponse,
)
from app.shared.core.deps import RequireOwner
from app.shared.core.rate_limit import client_ip, enforce_rate_limit

router = APIRouter(prefix="/billing", tags=["billing"])
log = logging.getLogger(__name__)


@router.get("/config", response_model=BillingConfigResponse)
def billing_config():
    enabled = wompi_enabled()
    return BillingConfigResponse(
        enabled=enabled,
        public_key=settings.wompi_public_key if enabled else None,
        sandbox=wompi_is_sandbox() if enabled else False,
        sync_enabled=wompi_sync_enabled(),
        auto_renew_enabled=wompi_sync_enabled(),
        api_base=wompi_api_base() if enabled else None,
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
    enforce_rate_limit(
        f"billing:checkout:{current.id}",
        limit=settings.rate_limit_billing_per_hour,
        window_seconds=3600,
        message="Demasiados intentos de pago. Espera un momento.",
    )

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

    if checkout.status == "pending" and wompi_sync_enabled() and sync_checkout_by_reference(db, checkout=checkout):
        db.commit()
        db.refresh(checkout)
    return _checkout_status_response(checkout)


def _checkout_status_response(checkout) -> CheckoutStatusResponse:
    plan_slug = checkout.plan.slug if checkout.plan else None
    plan_name = checkout.plan.name if checkout.plan else None
    return CheckoutStatusResponse(
        reference=checkout.reference,
        status=checkout.status,
        plan_slug=plan_slug,
        plan_name=plan_name,
        paid_at=checkout.paid_at,
        wompi_transaction_id=checkout.wompi_transaction_id,
        failure_reason=checkout.failure_reason,
    )


@router.post("/checkout/{reference}/sync", response_model=CheckoutStatusResponse)
def sync_checkout(
    reference: str,
    body: CheckoutSyncRequest,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    if not wompi_sync_enabled():
        raise HTTPException(
            status_code=503,
            detail="Sincronización con Wompi no configurada (falta WOMPI_PRIVATE_KEY).",
        )

    checkout = get_checkout_for_tenant(
        db, tenant_id=current.tenant_id, reference=reference.strip()
    )
    if checkout is None:
        raise HTTPException(status_code=404, detail="Checkout no encontrado")

    if checkout.status != "pending":
        db.refresh(checkout)
        return _checkout_status_response(checkout)

    sync_checkout_with_wompi(
        db,
        checkout=checkout,
        transaction_id=body.transaction_id.strip(),
    )
    db.commit()
    db.refresh(checkout)
    return _checkout_status_response(checkout)


@router.get("/wompi/return", include_in_schema=False)
def wompi_return(
    request: Request,
    ref: str = Query(default="", max_length=80),
    id: str = Query(default="", max_length=80),
    db: Session = Depends(get_db),
):
    """Wompi devuelve aquí al cliente (sin sesión: viene de otro dominio). Se confirma el pago contra
    la API de Wompi y se le manda a la página de precios, donde sí tiene su sesión."""
    enforce_rate_limit(f"billing:wompi-return:{client_ip(request)}", limit=30, window_seconds=600)
    reference = ref.strip()
    checkout = get_checkout_by_reference(db, reference) if reference else None
    if checkout is not None and checkout.status == "pending" and wompi_sync_enabled():
        transaction_id = id.strip()
        try:
            confirmed = (
                sync_checkout_with_wompi(db, checkout=checkout, transaction_id=transaction_id)
                if transaction_id
                else sync_checkout_by_reference(db, checkout=checkout)
            )
            if confirmed:
                db.commit()
        except Exception:
            db.rollback()
            log.exception("No se pudo confirmar el pago al volver de Wompi ref=%s", reference)
    if checkout is None:
        return RedirectResponse(settings.wompi_checkout_redirect_url, status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(site_checkout_return_url(checkout.reference), status_code=status.HTTP_303_SEE_OTHER)


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

    if not (settings.wompi_events_secret or "").strip() or not verify_event_checksum(event, x_event_checksum):
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


def _client_ip(request: Request) -> Optional[str]:
    ip = client_ip(request)
    return None if ip in ("unknown", "") else ip


def _owned_subscription(db: Session, current):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    subscription = get_tenant_subscription(db, current.tenant_id)
    if tenant is None or subscription is None:
        raise HTTPException(status_code=404, detail="Suscripción no encontrada")
    return tenant, subscription


@router.get("/wompi-terms", response_model=WompiTermsResponse)
def wompi_terms(current: RequireOwner):
    if not wompi_enabled():
        raise HTTPException(status_code=503, detail="Pagos con Wompi no configurados.")
    enforce_rate_limit(f"billing:terms:{current.id}", limit=30, window_seconds=3600)
    try:
        tokens = get_acceptance_tokens()
    except WompiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return WompiTermsResponse(
        acceptance_permalink=tokens["acceptance_permalink"],
        personal_data_permalink=tokens["personal_data_permalink"],
    )


@router.post("/payment-method", response_model=PaymentMethodResponse)
def set_payment_method(
    body: PaymentMethodRequest,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    if not body.accept_auto_renew or not body.accept_wompi_terms:
        raise HTTPException(
            status_code=400,
            detail="Debes autorizar el cobro automático y aceptar los términos de Wompi.",
        )
    enforce_rate_limit(
        f"billing:method:{current.id}",
        limit=settings.rate_limit_billing_per_hour,
        window_seconds=3600,
        message="Demasiados intentos de pago. Espera un momento.",
    )
    tenant, subscription = _owned_subscription(db, current)
    try:
        charge = save_payment_method(
            db,
            tenant=tenant,
            user=current.user,
            subscription=subscription,
            source_type=body.type,
            token=body.token.strip(),
            label_hint={
                "brand": body.brand or "",
                "last_four": body.last_four or "",
                "phone_last_four": body.phone_last_four or "",
            },
            plan_slug=(body.plan_slug or "").strip().lower() or None,
            ip_address=_client_ip(request),
        )
    except AutoRenewError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(subscription)
    charge_response = None
    if charge is not None:
        db.refresh(charge)
        charge_response = _checkout_status_response(charge)
    return PaymentMethodResponse(
        auto_renew=subscription.auto_renew,
        payment_method_label=subscription.payment_method_label,
        charge=charge_response,
        charge_error=charge.failure_reason if charge is not None and charge.status == "declined" else None,
    )


@router.delete("/payment-method", status_code=status.HTTP_204_NO_CONTENT)
def delete_payment_method(request: Request, current: RequireOwner, db: Session = Depends(get_db)):
    _, subscription = _owned_subscription(db, current)
    try:
        remove_payment_method(db, subscription=subscription, user=current.user, ip_address=_client_ip(request))
    except AutoRenewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()


@router.post("/subscription/cancel", status_code=status.HTTP_204_NO_CONTENT)
def cancel_my_subscription(
    body: CancelSubscriptionRequest,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    enforce_rate_limit(f"billing:cancel:{current.id}", limit=20, window_seconds=3600)
    _, subscription = _owned_subscription(db, current)
    try:
        cancel_subscription(
            db,
            subscription=subscription,
            user=current.user,
            reason=body.reason.strip().lower(),
            feedback=body.feedback,
            ip_address=_client_ip(request),
        )
    except AutoRenewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()


@router.post("/subscription/resume", status_code=status.HTTP_204_NO_CONTENT)
def resume_my_subscription(request: Request, current: RequireOwner, db: Session = Depends(get_db)):
    _, subscription = _owned_subscription(db, current)
    try:
        resume_subscription(db, subscription=subscription, user=current.user, ip_address=_client_ip(request))
    except AutoRenewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy.orm import Session, joinedload

from app.application.billing.plan_service import get_plan_by_slug
from app.application.billing.subscription_service import (
    activate_paid_subscription,
    get_tenant_subscription,
    renewal_period,
    subscription_is_paid,
)
from app.application.billing.tenant_service import log_audit
from app.application.billing.wompi_service import (
    WompiError,
    build_integrity_signature,
    cop_to_wompi_cents,
    fetch_transaction,
    find_transaction_by_reference,
    wompi_enabled,
)
from app.config import settings
from app.domain.entities.payment_checkout import PaymentCheckout, PaymentCheckoutKind, PaymentCheckoutStatus
from app.domain.entities import Plan, Tenant, User
from app.domain.entities.enums import TenantPlan

log = logging.getLogger(__name__)

RENEWAL_WINDOW_DAYS = 7


class CheckoutError(ValueError):
    pass


def wompi_return_base_url() -> str:
    """API a la que Wompi devuelve al cliente tras pagar. Wompi rechaza volver a localhost o a IPs
    privadas, así que en desarrollo se usa lvh.me (dominio público que resuelve a 127.0.0.1)."""
    base = settings.app_public_url.rstrip("/")
    parsed = urlsplit(base)
    if not settings.is_production() and parsed.hostname in ("localhost", "127.0.0.1"):
        netloc = "lvh.me" + (f":{parsed.port}" if parsed.port else "")
        base = urlunsplit(parsed._replace(netloc=netloc))
    return base


def site_checkout_return_url(reference: str) -> str:
    return f"{settings.wompi_checkout_redirect_url.rstrip('/')}?checkout=done&ref={quote(reference)}"


def create_checkout(
    db: Session,
    *,
    tenant: Tenant,
    user: User,
    plan_slug: str,
) -> dict[str, Any]:
    if not wompi_enabled():
        raise CheckoutError(
            "Pagos con Wompi aún no están configurados en el servidor."
        )

    plan = get_plan_by_slug(db, plan_slug)
    if plan is None or not plan.is_public:
        raise CheckoutError("Plan no disponible")

    subscription = get_tenant_subscription(db, tenant.id)
    if subscription is None:
        raise CheckoutError("Suscripción no encontrada")

    period_end = subscription.current_period_end
    renewal_window_open = period_end is None or (
        (period_end if period_end.tzinfo else period_end.replace(tzinfo=timezone.utc))
        - datetime.now(timezone.utc)
        <= timedelta(days=RENEWAL_WINDOW_DAYS)
    )
    if (
        subscription_is_paid(subscription)
        and subscription.plan_id == plan.id
        and not renewal_window_open
    ):
        raise CheckoutError("Ya tienes este plan activo")

    amount_in_cents = cop_to_wompi_cents(plan.price_cop)
    reference = new_reference(tenant.id)
    integrity_signature = build_integrity_signature(reference, amount_in_cents)

    checkout = PaymentCheckout(
        tenant_id=tenant.id,
        plan_id=plan.id,
        reference=reference,
        amount_in_cents=amount_in_cents,
        currency="COP",
        status=PaymentCheckoutStatus.PENDING,
        customer_email=user.email.lower(),
    )
    db.add(checkout)
    db.flush()

    redirect_url = f"{wompi_return_base_url()}/api/v1/billing/wompi/return?ref={reference}"

    return {
        "reference": reference,
        "amount_in_cents": amount_in_cents,
        "currency": "COP",
        "public_key": settings.wompi_public_key,
        "integrity_signature": integrity_signature,
        "redirect_url": redirect_url,
        "plan_slug": plan.slug,
        "plan_name": plan.name,
        "customer_email": user.email,
        "customer_name": user.full_name or user.email.split("@")[0],
    }


def get_checkout_for_tenant(
    db: Session, *, tenant_id: Any, reference: str
) -> Optional[PaymentCheckout]:
    return (
        db.query(PaymentCheckout)
        .options(joinedload(PaymentCheckout.plan))
        .filter(
            PaymentCheckout.tenant_id == tenant_id,
            PaymentCheckout.reference == reference,
        )
        .first()
    )


def get_checkout_by_reference(db: Session, reference: str) -> Optional[PaymentCheckout]:
    return (
        db.query(PaymentCheckout)
        .filter(PaymentCheckout.reference == reference)
        .first()
    )


def fulfill_checkout(
    db: Session,
    *,
    checkout: PaymentCheckout,
    wompi_transaction_id: Optional[str],
    wompi_status: str,
    failure_reason: Optional[str] = None,
) -> bool:
    """Returns True if subscription was activated."""
    # Estados finales de Wompi: no se reprocesan (webhook y consulta pueden llegar ambos).
    if checkout.status in (PaymentCheckoutStatus.APPROVED, PaymentCheckoutStatus.DECLINED):
        return False

    normalized = (wompi_status or "").upper()
    if normalized != "APPROVED":
        if normalized in ("DECLINED", "VOIDED", "ERROR"):
            checkout.status = PaymentCheckoutStatus.DECLINED
            checkout.failure_reason = (failure_reason or normalized)[:300]
            if wompi_transaction_id:
                checkout.wompi_transaction_id = wompi_transaction_id
            if checkout.kind == PaymentCheckoutKind.RENEWAL:
                from app.application.billing.auto_renew_service import register_renewal_failure

                register_renewal_failure(db, checkout=checkout)
        return False

    tenant = db.query(Tenant).filter(Tenant.id == checkout.tenant_id).first()
    subscription = get_tenant_subscription(db, checkout.tenant_id)
    plan = db.query(Plan).filter(Plan.id == checkout.plan_id).first()
    if tenant is None or subscription is None or plan is None:
        log.error("Checkout %s missing tenant/subscription/plan", checkout.reference)
        return False

    now = datetime.now(timezone.utc)
    checkout.status = PaymentCheckoutStatus.APPROVED
    checkout.wompi_transaction_id = wompi_transaction_id
    checkout.paid_at = now

    period_start, period_end = renewal_period(subscription, now)
    activate_paid_subscription(
        db,
        tenant=tenant,
        subscription=subscription,
        plan=plan,
        wompi_transaction_id=wompi_transaction_id,
        period_start=period_start,
        period_end=period_end,
    )

    log_audit(
        db,
        tenant_id=tenant.id,
        user_id=None,
        action="subscription.activated",
        details={
            "plan_slug": plan.slug,
            "reference": checkout.reference,
            "wompi_transaction_id": wompi_transaction_id,
            "kind": checkout.kind,
        },
        ip_address=None,
    )
    if checkout.kind in (PaymentCheckoutKind.AUTO, PaymentCheckoutKind.RENEWAL):
        from app.application.billing.auto_renew_service import on_auto_charge_approved

        on_auto_charge_approved(db, checkout=checkout, subscription=subscription, plan=plan)
    return True


def transaction_matches_checkout(checkout: PaymentCheckout, transaction: dict[str, Any]) -> bool:
    """La transacción es exactamente la que cobramos: misma referencia, monto y moneda.

    Sin esto, un pago de $1.000 con nuestra referencia activaría un plan de $200.000.
    """
    reference = str(transaction.get("reference") or "").strip()
    try:
        amount = int(transaction.get("amount_in_cents"))
    except (TypeError, ValueError):
        amount = None
    currency = str(transaction.get("currency") or "").upper()
    ok = (
        reference == checkout.reference
        and amount == int(checkout.amount_in_cents)
        and currency == (checkout.currency or "COP").upper()
    )
    if not ok:
        log.warning(
            "Wompi tx no coincide con checkout %s: ref=%s monto=%s/%s moneda=%s",
            checkout.reference,
            reference,
            amount,
            checkout.amount_in_cents,
            currency,
        )
        from app.application.monitoring.dev_alerts import alert_dev

        alert_dev(
            f"wompi:mismatch:{checkout.reference}",
            "Pago Wompi que no coincide con el checkout (posible manipulación)",
            f"Checkout {checkout.reference}: ref={reference} monto={amount}/{checkout.amount_in_cents} "
            f"moneda={currency}. No se activó el plan.",
            severity="warning",
        )
    return ok


def sync_checkout_with_wompi(
    db: Session,
    *,
    checkout: PaymentCheckout,
    transaction_id: str,
) -> bool:
    """Poll Wompi API and activate subscription if payment approved."""
    transaction = fetch_transaction(transaction_id)
    if not transaction:
        return False

    if not transaction_matches_checkout(checkout, transaction):
        return False

    return fulfill_checkout(
        db,
        checkout=checkout,
        wompi_transaction_id=str(transaction.get("id") or transaction_id),
        wompi_status=str(transaction.get("status") or ""),
        failure_reason=transaction.get("status_message"),
    )


def sync_checkout_by_reference(db: Session, *, checkout: PaymentCheckout) -> bool:
    """Al volver de la página de Wompi sin id de transacción: se busca por nuestra referencia."""
    try:
        transaction = find_transaction_by_reference(checkout.reference)
    except WompiError:
        return False
    if not transaction or not transaction.get("id"):
        return False
    status = str(transaction.get("status") or "").upper()
    if status == "PENDING" or not transaction_matches_checkout(checkout, transaction):
        return False
    return fulfill_checkout(
        db,
        checkout=checkout,
        wompi_transaction_id=str(transaction["id"]),
        wompi_status=status,
        failure_reason=transaction.get("status_message"),
    )


def handle_wompi_event(db: Session, event: dict[str, Any]) -> None:
    event_type = event.get("event")
    if event_type != "transaction.updated":
        return

    transaction = (event.get("data") or {}).get("transaction") or {}
    reference = transaction.get("reference")
    if not reference:
        return

    checkout = get_checkout_by_reference(db, reference)
    if checkout is None:
        log.warning("Wompi event for unknown reference %s", reference)
        return

    # El evento viene firmado, pero la verdad es la API de Wompi: se vuelve a consultar.
    transaction_id = str(transaction.get("id") or "").strip()
    confirmed = fetch_transaction(transaction_id) if transaction_id else None
    if confirmed is not None:
        transaction = confirmed
    elif (transaction.get("status") or "").upper() == "APPROVED" and (settings.wompi_private_key or "").strip():
        # Aprobado sin poder confirmarlo: no se activa ahora; el barrido reconcilia luego.
        log.warning("No se pudo confirmar en Wompi la tx %s; se reintenta después", transaction_id)
        return

    if not transaction_matches_checkout(checkout, transaction):
        return

    fulfill_checkout(
        db,
        checkout=checkout,
        wompi_transaction_id=str(transaction.get("id") or transaction_id) or None,
        wompi_status=transaction.get("status") or "",
        failure_reason=transaction.get("status_message"),
    )


def new_reference(tenant_id: Any) -> str:
    token = secrets.token_hex(6)
    tenant_part = str(tenant_id).replace("-", "")[:8]
    return f"om-{tenant_part}-{token}"

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session, joinedload

from app.application.billing.plan_service import get_plan_by_slug
from app.application.billing.subscription_service import (
    activate_paid_subscription,
    get_tenant_subscription,
)
from app.application.billing.tenant_service import log_audit
from app.application.billing.wompi_service import (
    build_integrity_signature,
    cop_to_wompi_cents,
    wompi_enabled,
)
from app.config import settings
from app.domain.entities.payment_checkout import PaymentCheckout, PaymentCheckoutStatus
from app.domain.entities import Plan, SubscriptionStatus, Tenant, User
from app.domain.entities.enums import TenantPlan

log = logging.getLogger(__name__)


class CheckoutError(ValueError):
    pass


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

    if (
        subscription.status == SubscriptionStatus.ACTIVE.value
        and subscription.plan_id == plan.id
    ):
        raise CheckoutError("Ya tienes este plan activo")

    amount_in_cents = cop_to_wompi_cents(plan.price_cop)
    reference = _new_reference(tenant.id)
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

    redirect_url = (
        f"{settings.wompi_checkout_redirect_url.rstrip('/')}"
        f"?checkout=done&ref={reference}"
    )

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
) -> bool:
    """Returns True if subscription was activated."""
    if checkout.status == PaymentCheckoutStatus.APPROVED:
        return False

    normalized = (wompi_status or "").upper()
    if normalized != "APPROVED":
        if normalized in ("DECLINED", "VOIDED", "ERROR"):
            checkout.status = PaymentCheckoutStatus.DECLINED
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

    activate_paid_subscription(
        db,
        tenant=tenant,
        subscription=subscription,
        plan=plan,
        wompi_transaction_id=wompi_transaction_id,
        period_start=now,
        period_end=now + timedelta(days=30),
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
        },
        ip_address=None,
    )
    return True


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

    fulfill_checkout(
        db,
        checkout=checkout,
        wompi_transaction_id=transaction.get("id"),
        wompi_status=transaction.get("status") or "",
    )


def _new_reference(tenant_id: Any) -> str:
    token = secrets.token_hex(6)
    tenant_part = str(tenant_id).replace("-", "")[:8]
    return f"om-{tenant_part}-{token}"

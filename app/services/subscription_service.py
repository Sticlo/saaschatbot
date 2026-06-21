from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session, joinedload

from app.models import Plan, Subscription, SubscriptionStatus, Tenant, TenantPlan


def get_tenant_subscription(db: Session, tenant_id: Any) -> Optional[Subscription]:
    return (
        db.query(Subscription)
        .options(joinedload(Subscription.plan))
        .filter(Subscription.tenant_id == tenant_id)
        .first()
    )


def build_subscription_summary(
    tenant: Tenant, subscription: Subscription, plan: Plan
) -> dict[str, Any]:
    is_trial = subscription.status == SubscriptionStatus.TRIAL.value
    is_active_paid = subscription.status == SubscriptionStatus.ACTIVE.value

    trial_limit = plan.trial_bait_limit
    trial_used = tenant.trial_bait_used
    trial_remaining = max(0, trial_limit - trial_used)

    if is_trial:
        effective_daily_limit = 0
        can_send_outbound = trial_remaining > 0
        needs_payment = trial_remaining == 0
    elif is_active_paid:
        effective_daily_limit = plan.daily_bait_limit
        can_send_outbound = True
        needs_payment = False
    else:
        effective_daily_limit = 0
        can_send_outbound = False
        needs_payment = subscription.status in (
            SubscriptionStatus.PAST_DUE.value,
            SubscriptionStatus.CANCELLED.value,
        )

    return {
        "status": subscription.status,
        "tenant_plan": tenant.plan,
        "plan": plan,
        "is_trial": is_trial,
        "is_paid": is_active_paid,
        "trial_bait_limit": trial_limit,
        "trial_bait_used": trial_used,
        "trial_bait_remaining": trial_remaining,
        "daily_bait_limit": effective_daily_limit,
        "can_send_outbound": can_send_outbound,
        "needs_payment": needs_payment,
        "current_period_start": subscription.current_period_start,
        "current_period_end": subscription.current_period_end,
    }


def activate_paid_subscription(
    db: Session, tenant: Tenant, subscription: Subscription, plan: Plan
) -> None:
    """Para Fase 6 (Wompi). Centraliza la activación del plan pagado."""
    tenant.plan = TenantPlan.PAID.value
    tenant.daily_bait_limit = plan.daily_bait_limit
    subscription.status = SubscriptionStatus.ACTIVE.value

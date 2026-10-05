from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.domain.entities import Plan, Subscription, SubscriptionStatus, Tenant, TenantPlan
from app.infrastructure.cache.redis_client import cache_set, tenant_cache_key

log = logging.getLogger(__name__)

# El cobro automático se intenta desde un día antes de vencer, para no cortar el servicio.
CHARGE_LEAD = timedelta(days=1)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def subscription_is_paid(subscription: Optional[Subscription], now: Optional[datetime] = None) -> bool:
    """Pagado = activo y dentro del periodo pagado (más los días de gracia)."""
    if subscription is None or subscription.status != SubscriptionStatus.ACTIVE.value:
        return False
    end = subscription.current_period_end
    if end is None:
        return True
    now = now or datetime.now(timezone.utc)
    return now <= _aware(end) + timedelta(days=settings.subscription_grace_days)


def trial_is_running(subscription: Optional[Subscription], now: Optional[datetime] = None) -> bool:
    if subscription is None or subscription.status != SubscriptionStatus.TRIAL.value:
        return False
    end = getattr(subscription, "trial_ends_at", None)
    return end is None or (now or datetime.now(timezone.utc)) < _aware(end)


def trial_has_expired(subscription: Optional[Subscription], now: Optional[datetime] = None) -> bool:
    """Probó y nunca pagó: la prueba terminó y no tiene ningún periodo pagado."""
    end = getattr(subscription, "trial_ends_at", None)
    if subscription is None or end is None or subscription.current_period_end is not None:
        return False
    if subscription.status not in (SubscriptionStatus.TRIAL.value, SubscriptionStatus.PAST_DUE.value):
        return False
    return (now or datetime.now(timezone.utc)) >= _aware(end)


def trial_days_left(subscription: Optional[Subscription], now: Optional[datetime] = None) -> Optional[int]:
    if not trial_is_running(subscription, now) or subscription.trial_ends_at is None:
        return None
    remaining = _aware(subscription.trial_ends_at) - (now or datetime.now(timezone.utc))
    return max(1, -(-remaining // timedelta(days=1)))


def plan_features_apply(subscription: Optional[Subscription]) -> bool:
    """Los límites del plan valen en prueba vigente o pagando; no con la prueba o el pago vencidos."""
    return trial_is_running(subscription) or subscription_is_paid(subscription)


def service_lapsed(subscription: Optional[Subscription]) -> bool:
    """Sin prueba vigente ni plan pagado: la IA deja de trabajar hasta que paguen."""
    return subscription is not None and not plan_features_apply(subscription)


def expire_lapsed_subscriptions(db: Session, now: Optional[datetime] = None) -> int:
    """Pasa a «pago pendiente» las suscripciones cuyo periodo + gracia ya terminó."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=settings.subscription_grace_days)
    lapsed = (
        db.query(Subscription)
        .filter(
            Subscription.status == SubscriptionStatus.ACTIVE.value,
            Subscription.current_period_end.isnot(None),
            Subscription.current_period_end < cutoff,
        )
        .all()
    )
    for subscription in lapsed:
        subscription.status = SubscriptionStatus.PAST_DUE.value
        tenant = db.get(Tenant, subscription.tenant_id)
        if tenant is not None:
            tenant.plan = TenantPlan.SUSPENDED.value
            tenant.daily_bait_limit = 0
            cache_set(tenant_cache_key(str(tenant.id), "plan"), tenant.plan, ttl_seconds=900)
        log.info("Suscripción vencida tenant=%s (venció %s)", subscription.tenant_id, subscription.current_period_end)
    if lapsed:
        db.commit()
    return len(lapsed)


def renewal_period(subscription: Subscription, now: datetime) -> tuple[datetime, datetime]:
    """Renovar antes de vencer suma 30 días al final actual; si ya venció, cuenta desde hoy."""
    end = subscription.current_period_end
    start = now
    if subscription_is_paid(subscription, now) and end is not None and _aware(end) > now:
        start = _aware(end)
    return start, start + timedelta(days=30)


def get_tenant_subscription(db: Session, tenant_id: Any) -> Optional[Subscription]:
    return (
        db.query(Subscription)
        .options(joinedload(Subscription.plan))
        .filter(Subscription.tenant_id == tenant_id)
        .first()
    )


def next_charge_at(subscription: Subscription) -> Optional[datetime]:
    if not subscription.auto_renew or subscription.cancel_at_period_end or not subscription.payment_source_id:
        return None
    if subscription.next_renewal_attempt_at is not None:
        return _aware(subscription.next_renewal_attempt_at)
    if subscription.current_period_end is None:
        return None
    return _aware(subscription.current_period_end) - CHARGE_LEAD


def build_subscription_summary(
    tenant: Tenant, subscription: Subscription, plan: Plan
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    is_trial = trial_is_running(subscription, now)
    trial_expired = trial_has_expired(subscription, now)
    is_active_paid = subscription_is_paid(subscription, now)
    lapsed = (subscription.status == SubscriptionStatus.ACTIVE.value and not is_active_paid) or (
        subscription.status == SubscriptionStatus.TRIAL.value and trial_expired
    )

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
        needs_payment = lapsed or subscription.status in (
            SubscriptionStatus.PAST_DUE.value,
            SubscriptionStatus.CANCELLED.value,
        )

    return {
        "status": SubscriptionStatus.PAST_DUE.value if lapsed else subscription.status,
        "tenant_plan": tenant.plan,
        "plan": plan,
        "is_trial": is_trial,
        "is_paid": is_active_paid,
        "trial_ends_at": subscription.trial_ends_at,
        "trial_days_left": trial_days_left(subscription, now),
        "trial_expired": trial_expired,
        "trial_bait_limit": trial_limit,
        "trial_bait_used": trial_used,
        "trial_bait_remaining": trial_remaining,
        "daily_bait_limit": effective_daily_limit,
        "can_send_outbound": can_send_outbound,
        "needs_payment": needs_payment,
        "current_period_start": subscription.current_period_start,
        "current_period_end": subscription.current_period_end,
        "auto_renew": bool(subscription.auto_renew),
        "payment_method_type": subscription.payment_method_type,
        "payment_method_label": subscription.payment_method_label,
        "next_charge_at": next_charge_at(subscription),
        "renewal_failing": (subscription.renewal_attempts or 0) > 0,
        "cancel_at_period_end": bool(subscription.cancel_at_period_end),
        "cancelled_at": subscription.cancelled_at,
    }


def activate_paid_subscription(
    db: Session,
    tenant: Tenant,
    subscription: Subscription,
    plan: Plan,
    *,
    wompi_transaction_id: Optional[str] = None,
    period_start: Optional[datetime] = None,
    period_end: Optional[datetime] = None,
) -> None:
    """Activa el plan pagado tras confirmación de Wompi."""
    tenant.plan = TenantPlan.PAID.value
    tenant.daily_bait_limit = plan.daily_bait_limit
    subscription.plan_id = plan.id
    subscription.status = SubscriptionStatus.ACTIVE.value
    if period_start is not None:
        subscription.current_period_start = period_start
    if period_end is not None:
        subscription.current_period_end = period_end
    if wompi_transaction_id and not subscription.wompi_customer_id:
        subscription.wompi_customer_id = wompi_transaction_id
    cache_set(tenant_cache_key(str(tenant.id), "plan"), tenant.plan, ttl_seconds=900)

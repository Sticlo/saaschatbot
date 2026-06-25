from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.domain.entities import Plan, Subscription, SubscriptionStatus, Tenant
from app.infrastructure.cache.redis_client import get_redis, tenant_cache_key
from app.application.billing.subscription_service import get_tenant_subscription


def _daily_key(tenant_id: uuid.UUID) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return tenant_cache_key(str(tenant_id), f"bait_daily:{day}")


def get_daily_bait_sent(tenant_id: uuid.UUID) -> int:
    raw = get_redis().get(_daily_key(tenant_id))
    return int(raw) if raw else 0


def increment_daily_bait_sent(tenant_id: uuid.UUID) -> int:
    client = get_redis()
    key = _daily_key(tenant_id)
    count = client.incr(key)
    if count == 1:
        client.expire(key, 86400 * 2)
    return count


def check_bait_quota(
    db: Session,
    tenant: Tenant,
    *,
    count: int = 1,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Verifica si el tenant puede enviar `count` carnadas más.
    Retorna (ok, reason, meta).
    """
    subscription = get_tenant_subscription(db, tenant.id)
    if subscription is None or subscription.plan is None:
        return False, "Suscripción no encontrada", {}

    plan: Plan = subscription.plan
    is_trial = subscription.status == SubscriptionStatus.TRIAL.value
    is_paid = subscription.status == SubscriptionStatus.ACTIVE.value

    if not tenant.disclaimer_accepted_at:
        return False, "Debes aceptar el disclaimer antes de enviar carnadas", {}

    if is_trial:
        remaining = max(0, plan.trial_bait_limit - tenant.trial_bait_used)
        if count > 0 and remaining < count:
            return (
                False,
                f"Trial agotado: te quedan {remaining} carnadas de {plan.trial_bait_limit}",
                {"trial_remaining": remaining, "trial_limit": plan.trial_bait_limit},
            )
        return True, "", {
            "is_trial": True,
            "trial_remaining": remaining,
            "trial_limit": plan.trial_bait_limit,
        }

    if is_paid:
        daily_limit = plan.daily_bait_limit
        sent_today = get_daily_bait_sent(tenant.id)
        remaining_today = max(0, daily_limit - sent_today)
        if count > 0 and remaining_today < count:
            return (
                False,
                f"Límite diario alcanzado ({sent_today}/{daily_limit} hoy)",
                {"daily_sent": sent_today, "daily_limit": daily_limit},
            )
        return True, "", {
            "is_trial": False,
            "daily_sent": sent_today,
            "daily_limit": daily_limit,
            "daily_remaining": remaining_today,
        }

    return False, "Activa tu plan para enviar carnadas", {"needs_payment": True}


def record_bait_sent(db: Session, tenant: Tenant) -> None:
    """Incrementa contadores tras un envío exitoso."""
    subscription = get_tenant_subscription(db, tenant.id)
    if subscription is None or subscription.plan is None:
        return

    if subscription.status == SubscriptionStatus.TRIAL.value:
        tenant.trial_bait_used = (tenant.trial_bait_used or 0) + 1
    elif subscription.status == SubscriptionStatus.ACTIVE.value:
        increment_daily_bait_sent(tenant.id)


def build_limits_summary(db: Session, tenant: Tenant) -> dict[str, Any]:
    ok, reason, meta = check_bait_quota(db, tenant, count=1)
    subscription = get_tenant_subscription(db, tenant.id)
    plan = subscription.plan if subscription else None

    base: dict[str, Any] = {
        "can_send_bait": ok,
        "block_reason": reason or None,
        "disclaimer_accepted": tenant.disclaimer_accepted_at is not None,
        "trial_bait_used": tenant.trial_bait_used,
        "trial_bait_limit": plan.trial_bait_limit if plan else 0,
        "trial_bait_remaining": max(0, (plan.trial_bait_limit if plan else 0) - tenant.trial_bait_used),
        "daily_bait_sent": get_daily_bait_sent(tenant.id),
        "daily_bait_limit": plan.daily_bait_limit if plan else 0,
    }
    base.update(meta)
    return base

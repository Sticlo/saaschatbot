from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.domain.entities import Subscription, SubscriptionStatus, Tenant
from app.infrastructure.cache.redis_client import get_redis


def _reply_day_key(tenant_id: uuid.UUID) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"tenant:{tenant_id}:ai_replies:{day}"


def _classify_day_key(tenant_id: uuid.UUID) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"tenant:{tenant_id}:ai_classify:{day}"


def _plan_feature_int(plan_features: dict, key: str) -> Optional[int]:
    custom = plan_features.get(key)
    if custom is None:
        return None
    if custom == "unlimited" or custom is True:
        return 0
    try:
        return max(0, int(custom))
    except (TypeError, ValueError):
        return None


def daily_reply_limit(db: Session, tenant: Tenant) -> int:
    sub = (
        db.query(Subscription)
        .options(joinedload(Subscription.plan))
        .filter(Subscription.tenant_id == tenant.id)
        .first()
    )
    if sub and sub.plan:
        features = sub.plan.features if isinstance(sub.plan.features, dict) else {}
        custom = _plan_feature_int(features, "ai_daily_replies")
        if custom is not None:
            return custom
    if sub and sub.status == SubscriptionStatus.ACTIVE.value:
        return settings.ai_paid_daily_reply_limit
    return settings.ai_trial_daily_reply_limit


def daily_classify_limit(db: Session, tenant: Tenant) -> int:
    """0 = ilimitado (plan pagado — clasificar es el core del producto)."""
    sub = (
        db.query(Subscription)
        .options(joinedload(Subscription.plan))
        .filter(Subscription.tenant_id == tenant.id)
        .first()
    )
    if sub and sub.plan:
        features = sub.plan.features if isinstance(sub.plan.features, dict) else {}
        custom = _plan_feature_int(features, "ai_daily_classifications")
        if custom is not None:
            return custom
    if sub and sub.status == SubscriptionStatus.ACTIVE.value:
        return settings.ai_paid_daily_classify_limit
    return settings.ai_trial_daily_classify_limit


def get_daily_reply_count(tenant_id: uuid.UUID) -> int:
    try:
        raw = get_redis().get(_reply_day_key(tenant_id))
        return int(raw or 0)
    except Exception:
        return 0


def get_daily_classify_count(tenant_id: uuid.UUID) -> int:
    try:
        raw = get_redis().get(_classify_day_key(tenant_id))
        return int(raw or 0)
    except Exception:
        return 0


def increment_daily_reply_count(tenant_id: uuid.UUID) -> int:
    key = _reply_day_key(tenant_id)
    client = get_redis()
    count = int(client.incr(key))
    if count == 1:
        client.expire(key, 90000)
    return count


def increment_daily_classify_count(tenant_id: uuid.UUID) -> int:
    key = _classify_day_key(tenant_id)
    client = get_redis()
    count = int(client.incr(key))
    if count == 1:
        client.expire(key, 90000)
    return count


def check_daily_reply_quota(
    db: Session, tenant: Tenant
) -> tuple[bool, int, int, Optional[str]]:
    limit = daily_reply_limit(db, tenant)
    used = get_daily_reply_count(tenant.id)
    if limit == 0:
        return True, used, 0, None
    if limit < 0:
        return False, 0, 0, "Respuestas IA no incluidas en tu plan"
    if used >= limit:
        return (
            False,
            used,
            limit,
            f"Límite diario de respuestas IA ({used}/{limit}). Mañana se reinicia.",
        )
    return True, used, limit, None


def check_daily_classify_quota(
    db: Session, tenant: Tenant
) -> tuple[bool, int, int, Optional[str]]:
    limit = daily_classify_limit(db, tenant)
    used = get_daily_classify_count(tenant.id)
    if limit == 0:
        return True, used, 0, None
    if used >= limit:
        return (
            False,
            used,
            limit,
            f"Límite diario de clasificación ({used}/{limit}). Activa tu plan para IA ilimitada.",
        )
    return True, used, limit, None


def build_ai_usage_summary(db: Session, tenant: Tenant) -> dict:
    reply_limit = daily_reply_limit(db, tenant)
    reply_used = get_daily_reply_count(tenant.id)
    classify_limit = daily_classify_limit(db, tenant)
    classify_used = get_daily_classify_count(tenant.id)
    classify_unlimited = classify_limit == 0
    reply_unlimited = reply_limit == 0
    return {
        "daily_replies_used": reply_used,
        "daily_replies_limit": reply_limit,
        "daily_replies_unlimited": reply_unlimited,
        "daily_replies_remaining": (
            None if reply_unlimited else max(0, reply_limit - reply_used)
        ),
        "daily_classifications_used": classify_used,
        "daily_classifications_limit": classify_limit,
        "daily_classifications_unlimited": classify_unlimited,
        "daily_classifications_remaining": (
            None if classify_unlimited else max(0, classify_limit - classify_used)
        ),
    }

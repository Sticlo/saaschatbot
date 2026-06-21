from __future__ import annotations

import uuid
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Subscription,
    SubscriptionStatus,
    Tenant,
    TenantPlan,
    TenantProfile,
    User,
    UserRole,
)
from app.core.security import slugify
from app.services.plan_service import get_default_plan


def _unique_slug(db: Session, business_name: str) -> str:
    base = slugify(business_name)[:80]
    slug = base
    counter = 1
    while db.query(Tenant).filter(Tenant.slug == slug).first():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def register_tenant_with_owner(
    db: Session,
    *,
    business_name: str,
    owner_name: str,
    email: str,
    hashed_password: str,
) -> Tuple[Tenant, User]:
    slug = _unique_slug(db, business_name)
    plan = get_default_plan(db)
    tenant = Tenant(
        business_name=business_name,
        slug=slug,
        plan=TenantPlan.TRIAL.value,
        daily_bait_limit=plan.trial_bait_limit,
    )
    db.add(tenant)
    db.flush()

    profile = TenantProfile(tenant_id=tenant.id, onboarding_answers={})
    subscription = Subscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        status=SubscriptionStatus.TRIAL.value,
    )
    owner = User(
        tenant_id=tenant.id,
        email=email.lower(),
        hashed_password=hashed_password,
        full_name=owner_name,
        role=UserRole.OWNER.value,
    )
    db.add_all([profile, subscription, owner])
    db.flush()
    return tenant, owner


def log_audit(
    db: Session,
    *,
    tenant_id: Optional[uuid.UUID],
    user_id: Optional[uuid.UUID],
    action: str,
    details: Optional[dict] = None,
    message: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> AuditLog:
    entry = AuditLog(
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        details=details,
        message=message,
        ip_address=ip_address,
    )
    db.add(entry)
    return entry

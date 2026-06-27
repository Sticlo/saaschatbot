from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.domain.entities import TenantProfile

_BUSINESS_FIELDS = (
    "industry",
    "products_services",
    "target_customer",
    "price_range",
    "location_hours",
    "tone",
    "restrictions",
    "maps_prospect_business",
    "maps_prospect_city",
)


def answers_from_profile(profile: TenantProfile) -> dict[str, str]:
    """Lee respuestas del negocio desde columnas dedicadas (fallback JSON legacy)."""
    out: dict[str, str] = {}
    raw = profile.onboarding_answers if isinstance(profile.onboarding_answers, dict) else {}
    for key in _BUSINESS_FIELDS:
        col_val = getattr(profile, key, None)
        if col_val is not None and str(col_val).strip():
            out[key] = str(col_val).strip()
        elif raw.get(key):
            out[key] = str(raw[key]).strip()
    return out


def apply_business_answers(profile: TenantProfile, answers: dict[str, str]) -> None:
    """Persiste en columnas y mantiene onboarding_answers sincronizado."""
    merged = answers_from_profile(profile)
    merged.update({k: v.strip() for k, v in answers.items() if v is not None})
    for key in _BUSINESS_FIELDS:
        value = merged.get(key, "")
        setattr(profile, key, value or None)
    profile.onboarding_answers = {
        k: merged[k]
        for k in (
            "industry",
            "products_services",
            "target_customer",
            "price_range",
            "location_hours",
            "tone",
            "restrictions",
        )
        if merged.get(k)
    }


def get_or_create_tenant_profile(db: Session, tenant_id: uuid.UUID) -> TenantProfile:
    profile = (
        db.query(TenantProfile)
        .filter(TenantProfile.tenant_id == tenant_id)
        .first()
    )
    if profile is None:
        profile = TenantProfile(tenant_id=tenant_id, onboarding_answers={})
        db.add(profile)
        db.flush()
    return profile

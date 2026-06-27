from __future__ import annotations

from typing import Optional

from app.config import settings
from app.domain.entities import TenantProfile
from app.domain.entities.enums import AiMode


def resolve_ai_mode(profile: Optional[TenantProfile]) -> str:
    raw = (profile.ai_mode if profile else None) or settings.ai_default_mode
    cleaned = str(raw).strip().lower()
    if cleaned == AiMode.FULL_REPLY.value:
        return AiMode.FULL_REPLY.value
    if cleaned == AiMode.CLASSIFY_ONLY.value:
        return AiMode.CLASSIFY_ONLY.value
    return AiMode.QUALIFY.value


def is_classify_only(profile: Optional[TenantProfile]) -> bool:
    return resolve_ai_mode(profile) == AiMode.CLASSIFY_ONLY.value


def is_qualify_mode(profile: Optional[TenantProfile]) -> bool:
    return resolve_ai_mode(profile) == AiMode.QUALIFY.value


def uses_ai_replies(profile: Optional[TenantProfile]) -> bool:
    return not is_classify_only(profile)

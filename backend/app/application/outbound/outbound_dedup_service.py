from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.domain.entities import Exclusion, Lead, Subscription, SubscriptionStatus, Tenant
from app.domain.entities.enums import LeadStatus
from app.infrastructure.cache.redis_client import get_redis, tenant_cache_key

log = logging.getLogger(__name__)

SET_ALL = "phones:all"
SET_CONTACTED = "phones:contacted"
SET_EXCLUDED = "phones:excluded"


def _set_key(tenant_id: uuid.UUID, suffix: str) -> str:
    return tenant_cache_key(str(tenant_id), suffix)


def ensure_dedup_sets_loaded(db: Session, tenant_id: uuid.UUID) -> None:
    """Carga sets Redis desde DB si aún no existen (primera vez)."""
    client = get_redis()
    marker = _set_key(tenant_id, "dedup_loaded")
    if client.get(marker):
        return

    all_key = _set_key(tenant_id, SET_ALL)
    contacted_key = _set_key(tenant_id, SET_CONTACTED)
    excluded_key = _set_key(tenant_id, SET_EXCLUDED)

    phones = [row.phone_e164 for row in db.query(Lead.phone_e164).filter(Lead.tenant_id == tenant_id).all()]
    if phones:
        client.sadd(all_key, *phones)

    excluded = [
        row.phone_e164
        for row in db.query(Exclusion.phone_e164).filter(Exclusion.tenant_id == tenant_id).all()
    ]
    if excluded:
        client.sadd(excluded_key, *excluded)

    contacted = [
        row.phone_e164
        for row in db.query(Lead.phone_e164)
        .filter(Lead.tenant_id == tenant_id, Lead.status == LeadStatus.CONTACTED.value)
        .all()
    ]
    if contacted:
        client.sadd(contacted_key, *contacted)

    client.set(marker, "1", ex=86400 * 7)


def is_phone_known(tenant_id: uuid.UUID, phone: str) -> bool:
    return bool(get_redis().sismember(_set_key(tenant_id, SET_ALL), phone))


def is_phone_contacted(tenant_id: uuid.UUID, phone: str) -> bool:
    return bool(get_redis().sismember(_set_key(tenant_id, SET_CONTACTED), phone))


def is_phone_excluded(tenant_id: uuid.UUID, phone: str) -> bool:
    return bool(get_redis().sismember(_set_key(tenant_id, SET_EXCLUDED), phone))


def register_phone(tenant_id: uuid.UUID, phone: str) -> None:
    get_redis().sadd(_set_key(tenant_id, SET_ALL), phone)


def mark_phone_contacted(tenant_id: uuid.UUID, phone: str) -> None:
    client = get_redis()
    client.sadd(_set_key(tenant_id, SET_ALL), phone)
    client.sadd(_set_key(tenant_id, SET_CONTACTED), phone)


def mark_phone_excluded(tenant_id: uuid.UUID, phone: str) -> None:
    client = get_redis()
    client.sadd(_set_key(tenant_id, SET_ALL), phone)
    client.sadd(_set_key(tenant_id, SET_EXCLUDED), phone)


def should_skip_outbound(
    db: Session,
    tenant_id: uuid.UUID,
    phone: str,
) -> Optional[str]:
    """Devuelve razón de skip o None si puede enviarse."""
    ensure_dedup_sets_loaded(db, tenant_id)

    if is_phone_excluded(tenant_id, phone):
        return "excluded"

    if is_phone_contacted(tenant_id, phone):
        return "already_contacted"

    from app.domain.entities import Conversation

    existing = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.contact_phone == phone,
            Conversation.bait_sent.is_(True),
        )
        .first()
    )
    if existing:
        mark_phone_contacted(tenant_id, phone)
        return "bait_already_sent"

    return None

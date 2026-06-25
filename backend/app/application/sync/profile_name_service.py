from __future__ import annotations

import json
import logging
import time
from typing import Optional

from app.domain.entities import Conversation
from app.infrastructure.cache.redis_client import cache_get, cache_set
from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client
from app.shared.core.phone import (
    is_owner_display_name,
    is_placeholder_contact_name,
    is_valid_whatsapp_phone,
    phone_to_evolution_number,
)

log = logging.getLogger(__name__)

PROFILE_CACHE_TTL = 86_400


def _profile_cache_key(instance_name: str, digits: str) -> str:
    return f"wa:profile:v1:{instance_name}:{digits}"


def fetch_whatsapp_profile_name(
    instance_name: str,
    phone: str,
    *,
    use_cache: bool = True,
) -> str:
    """Consulta Evolution fetchProfile — nombre público del perfil de WhatsApp."""
    if not is_valid_whatsapp_phone(phone):
        return ""
    digits = phone_to_evolution_number(phone)
    cache_key = _profile_cache_key(instance_name, digits)
    if use_cache:
        raw = cache_get(cache_key)
        if raw is not None:
            try:
                return str(json.loads(raw).get("name") or "").strip()
            except (json.JSONDecodeError, TypeError):
                pass

    name = ""
    try:
        payload = evolution_client.fetch_profile(instance_name, digits)
        if isinstance(payload, dict):
            name = str(payload.get("name") or "").strip()
            if not name and payload.get("isBusiness"):
                try:
                    biz = evolution_client.fetch_business_profile(instance_name, digits)
                    if isinstance(biz, dict):
                        name = str(biz.get("description") or biz.get("name") or "").strip()
                except EvolutionAPIError:
                    pass
    except EvolutionAPIError as exc:
        log.debug("fetchProfile %s: %s", digits, exc)

    cache_set(cache_key, json.dumps({"name": name}), ttl_seconds=PROFILE_CACHE_TTL)
    return name


def enrich_names_from_whatsapp_profiles(
    conversations: list[Conversation],
    *,
    instance_name: str,
    owner_names: Optional[set[str]] = None,
    limit: int = 40,
    pause_ms: int = 120,
) -> dict[str, int]:
    """Pide nombre de perfil WA a Evolution para chats que siguen sin nombre."""
    owner_names = owner_names or set()
    candidates = [
        conv
        for conv in conversations
        if is_placeholder_contact_name(conv.contact_name, conv.contact_phone)
        and is_valid_whatsapp_phone(conv.contact_phone)
    ]
    candidates.sort(
        key=lambda conv: conv.last_message_at.timestamp() if conv.last_message_at else 0,
        reverse=True,
    )
    candidates = candidates[: max(0, limit)]

    stats = {"fetched": 0, "fixed": 0}
    for conv in candidates:
        name = fetch_whatsapp_profile_name(instance_name, conv.contact_phone)
        stats["fetched"] += 1
        if pause_ms > 0:
            time.sleep(pause_ms / 1000.0)
        if not name or is_owner_display_name(name, owner_names):
            continue
        if is_placeholder_contact_name(name, conv.contact_phone):
            continue
        if conv.contact_name != name:
            conv.contact_name = name
            stats["fixed"] += 1
    return stats

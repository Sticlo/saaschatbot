from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from app.domain.entities.enums import WhatsAppStatus

log = logging.getLogger(__name__)

# Códigos/statusReason comunes en Baileys/Evolution que indican restricción
_BANNED_HINTS = ("ban", "blocked", "forbidden", "logout")
_RESTRICTED_HINTS = ("restrict", "limit", "429", "503")


def resolve_whatsapp_status(state: str, data: Optional[dict] = None) -> str:
    """Mapea estado Evolution → WhatsAppStatus del tenant."""
    normalized = (state or "").lower()
    data = data or {}
    reason_raw = str(
        data.get("statusReason")
        or data.get("reason")
        or data.get("message")
        or ""
    ).lower()

    combined = f"{normalized} {reason_raw}"
    if any(h in combined for h in _BANNED_HINTS):
        return WhatsAppStatus.BANNED.value
    if any(h in combined for h in _RESTRICTED_HINTS):
        return WhatsAppStatus.RESTRICTED.value

    if normalized in {"open", "connected"}:
        return WhatsAppStatus.CONNECTED.value
    if normalized in {"connecting", "pairing", "qrcode"}:
        return WhatsAppStatus.CONNECTING.value
    return WhatsAppStatus.DISCONNECTED.value


def apply_session_status(
    session,
    tenant,
    mapped_status: str,
    *,
    owner_jid: Optional[str] = None,
) -> None:
    from app.shared.core.phone import jid_to_phone
    from app.infrastructure.cache.redis_client import cache_set, tenant_cache_key

    session.status = mapped_status
    tenant.whatsapp_status = mapped_status
    now = datetime.now(timezone.utc)

    if mapped_status == WhatsAppStatus.CONNECTED.value:
        session.last_connected_at = now
        session.qr_base64 = None
        if owner_jid:
            session.phone_number = jid_to_phone(str(owner_jid))
            session.bound_owner_jid = str(owner_jid)
    elif mapped_status in {
        WhatsAppStatus.DISCONNECTED.value,
        WhatsAppStatus.BANNED.value,
        WhatsAppStatus.RESTRICTED.value,
    }:
        session.last_disconnected_at = now
        session.phone_number = None
        session.qr_base64 = None
        if mapped_status != WhatsAppStatus.DISCONNECTED.value:
            log.warning(
                "WhatsApp tenant=%s status=%s — outbound debe pausarse",
                tenant.id,
                mapped_status,
            )

    cache_set(
        tenant_cache_key(str(tenant.id), "whatsapp_status"),
        mapped_status,
        ttl_seconds=300,
    )


def can_send_whatsapp(status: str) -> Tuple[bool, str]:
    if status == WhatsAppStatus.CONNECTED.value:
        return True, ""
    if status == WhatsAppStatus.CONNECTING.value:
        return False, "WhatsApp aún conectando — espera a escanear el QR"
    if status == WhatsAppStatus.BANNED.value:
        return False, "WhatsApp restringido o baneado — reconecta bajo tu responsabilidad"
    if status == WhatsAppStatus.RESTRICTED.value:
        return False, "WhatsApp con restricciones temporales — intenta más tarde"
    return False, "WhatsApp no conectado — escanea el QR para reconectar"

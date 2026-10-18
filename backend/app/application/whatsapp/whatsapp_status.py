from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from app.domain.entities.enums import WhatsAppStatus

log = logging.getLogger(__name__)

# Códigos/statusReason comunes en Baileys/Evolution que indican restricción.
# «logout» no va aquí: desvincular desde el celular es una desconexión normal, no un bloqueo.
_BANNED_HINTS = ("ban", "blocked", "forbidden")
_RESTRICTED_HINTS = ("restrict", "limit", "429", "503")


# Códigos de desconexión de Baileys: separan un bloqueo real de una caída o una desvinculación.
DISCONNECT_REASONS = {
    "banned": "WhatsApp bloqueó o restringió el número",
    "device_removed": "WhatsApp quitó el dispositivo vinculado (o lo desvincularon desde el celular)",
    "replaced": "La sesión se abrió en otro lado",
    "network": "Caída de conexión (suele recuperarse sola)",
    "silent": "Se cayó sin avisar (lo detectó la revisión periódica)",
    "unknown": "Motivo desconocido",
}
_CODE_REASONS = {403: "banned", 402: "banned", 401: "device_removed", 411: "device_removed", 440: "replaced"}
_TRANSIENT_CODES = {408, 428, 500, 503, 515}


def _disconnect_code(data: dict) -> Optional[int]:
    candidates = [data.get("statusReason"), data.get("statusCode")]
    last = data.get("lastDisconnect")
    if isinstance(last, dict):
        error = last.get("error")
        if isinstance(error, dict):
            output = error.get("output")
            candidates.append(output.get("statusCode") if isinstance(output, dict) else None)
    for value in candidates:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def classify_disconnect(data: Optional[dict]) -> Tuple[str, Optional[int]]:
    """(motivo, código) de un `connection.update` que cerró la sesión."""
    data = data or {}
    code = _disconnect_code(data)
    if code in _CODE_REASONS:
        return _CODE_REASONS[code], code
    if code in _TRANSIENT_CODES:
        return "network", code
    raw = str(data).lower()
    if "device_removed" in raw:
        return "device_removed", code
    if "forbidden" in raw or "banned" in raw:
        return "banned", code
    return "unknown", code


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
    if normalized not in {"open", "connected"} and _disconnect_code(data) == 403:
        return WhatsAppStatus.BANNED.value
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
        from app.application.whatsapp.whatsapp_health_service import clear_down_mark

        clear_down_mark(tenant.id)
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


def build_owner_display_names(session) -> set[str]:
    """Nombres del dueño de la sesión WA — nunca deben usarse como nombre de contacto."""
    from app.config import settings
    from app.shared.core.phone import phone_to_evolution_number
    from app.infrastructure.cache.redis_client import cache_get, cache_set, tenant_cache_key

    names: set[str] = set()
    if session is None:
        return names

    dsn = settings.evolution_database_url
    instance = getattr(session, "instance_name", "") or ""

    cache_key = ""
    if instance:
        cache_key = tenant_cache_key(instance, "owner_display_names")
        cached = cache_get(cache_key)
        if cached is not None:
            return {n for n in str(cached).split("\n") if n.strip()}

    if dsn and instance:
        from app.infrastructure.evolution.evolution_store import (
            fetch_contact_push_name,
            fetch_owner_push_names,
        )

        if session.bound_owner_jid:
            push = fetch_contact_push_name(dsn, instance, session.bound_owner_jid)
            if push:
                names.add(push.strip())

        # Fuente confiable: el pushName de cualquier mensaje fromMe es el nombre del dueño.
        names.update(fetch_owner_push_names(dsn, instance))

    if instance and session.phone_number:
        try:
            from app.infrastructure.evolution.evolution_client import evolution_client

            profile = evolution_client.fetch_profile(
                instance, phone_to_evolution_number(session.phone_number)
            )
            profile_name = str((profile or {}).get("name") or "").strip()
            if profile_name:
                names.add(profile_name)
        except Exception:
            pass

    cleaned = {n.strip() for n in names if n and n.strip()}
    if cache_key and cleaned:
        cache_set(cache_key, "\n".join(sorted(cleaned)), ttl_seconds=300)
    return cleaned


def sanitize_leaked_owner_name(conversation, owner_names: set[str]) -> bool:
    """Quita el nombre del dueño si quedó guardado como contacto por error."""
    from app.shared.core.phone import (
        is_owner_display_name,
        is_placeholder_contact_name,
        is_valid_whatsapp_phone,
    )

    if not owner_names or not is_owner_display_name(conversation.contact_name, owner_names):
        return False
    if is_valid_whatsapp_phone(conversation.contact_phone):
        conversation.contact_name = conversation.contact_phone
    else:
        conversation.contact_name = ""
    return not is_placeholder_contact_name(conversation.contact_name, conversation.contact_phone)


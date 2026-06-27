from __future__ import annotations

import hashlib
import hmac
from typing import Any, Optional

from app.config import settings


def wompi_enabled() -> bool:
    return bool(
        (settings.wompi_public_key or "").strip()
        and (settings.wompi_integrity_secret or "").strip()
    )


def cop_to_wompi_cents(price_cop: int) -> int:
    """Wompi expects COP amounts in centavos (pesos × 100)."""
    return int(price_cop) * 100


def build_integrity_signature(reference: str, amount_in_cents: int, currency: str = "COP") -> str:
    secret = (settings.wompi_integrity_secret or "").strip()
    if not secret:
        raise ValueError("Wompi integrity secret no configurado")
    payload = f"{reference}{amount_in_cents}{currency}{secret}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_event_checksum(event: dict[str, Any], header_checksum: Optional[str]) -> bool:
    secret = (settings.wompi_events_secret or "").strip()
    if not secret:
        return False

    signature = event.get("signature") or {}
    properties = signature.get("properties") or []
    timestamp = event.get("timestamp")
    if timestamp is None or not properties:
        return False

    parts: list[str] = []
    data = event.get("data") or {}
    for prop in properties:
        value = _resolve_property(data, str(prop))
        if value is None:
            return False
        parts.append(str(value))

    payload = "".join(parts) + str(timestamp) + secret
    calculated = hashlib.sha256(payload.encode("utf-8")).hexdigest().upper()
    provided = (header_checksum or signature.get("checksum") or "").upper()
    if not provided:
        return False
    return hmac.compare_digest(calculated, provided)


def _resolve_property(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current

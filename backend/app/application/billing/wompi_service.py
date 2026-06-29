from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)


def wompi_enabled() -> bool:
    return bool(
        (settings.wompi_public_key or "").strip()
        and (settings.wompi_integrity_secret or "").strip()
    )


def wompi_sync_enabled() -> bool:
    return wompi_enabled() and bool((settings.wompi_private_key or "").strip())


def wompi_is_sandbox() -> bool:
    return "pub_test_" in (settings.wompi_public_key or "")


def wompi_api_base() -> str:
    configured = (settings.wompi_api_base_url or "").strip().rstrip("/")
    if configured:
        return configured
    if wompi_is_sandbox():
        return "https://sandbox.wompi.co/v1"
    return "https://production.wompi.co/v1"


def cop_to_wompi_cents(price_cop: int) -> int:
    """Wompi expects COP amounts in centavos (pesos × 100)."""
    return int(price_cop) * 100


def build_integrity_signature(reference: str, amount_in_cents: int, currency: str = "COP") -> str:
    secret = (settings.wompi_integrity_secret or "").strip()
    if not secret:
        raise ValueError("Wompi integrity secret no configurado")
    payload = f"{reference}{amount_in_cents}{currency}{secret}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fetch_transaction(transaction_id: str) -> Optional[dict[str, Any]]:
    """Fetch transaction status from Wompi API (requires private key)."""
    private_key = (settings.wompi_private_key or "").strip()
    if not private_key or not transaction_id:
        return None

    url = f"{wompi_api_base()}/transactions/{transaction_id.strip()}"
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.get(
                url,
                headers={"Authorization": f"Bearer {private_key}"},
            )
        if response.status_code >= 400:
            log.warning("Wompi transaction lookup %s: %s", response.status_code, response.text)
            return None
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else payload
    except httpx.HTTPError:
        log.exception("Wompi transaction lookup failed for %s", transaction_id)
        return None


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

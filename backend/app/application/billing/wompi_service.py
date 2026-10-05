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


class WompiError(RuntimeError):
    """Error de la API de Wompi con un mensaje apto para mostrar al usuario."""


def _private_key() -> str:
    key = (settings.wompi_private_key or "").strip()
    if not key:
        from app.application.monitoring.dev_alerts import alert_dev

        alert_dev("wompi:no_private_key", "Falta WOMPI_PRIVATE_KEY", "Los cobros automáticos están detenidos.", severity="critical")
        raise WompiError("Los pagos automáticos no están disponibles en este momento. Intenta más tarde.")
    return key


def _error_message(response: httpx.Response) -> str:
    try:
        error = (response.json() or {}).get("error") or {}
    except ValueError:
        return f"Wompi respondió {response.status_code}"
    messages = error.get("messages")
    if isinstance(messages, dict):
        flat = [f"{field}: {', '.join(map(str, errs))}" for field, errs in messages.items()]
        if flat:
            return "; ".join(flat)[:300]
    return str(error.get("reason") or error.get("type") or f"Wompi respondió {response.status_code}")[:300]


def _alert_wompi_down(detail: str) -> None:
    from app.application.monitoring.dev_alerts import alert_dev

    alert_dev(
        "wompi:api_error",
        "Wompi está fallando (no responde o rechaza nuestras credenciales)",
        f"{detail}\nLos pagos y cobros automáticos pueden estar fallando.",
        severity="critical",
    )


def _request(method: str, path: str, *, headers: dict[str, str], json: Optional[dict] = None) -> dict[str, Any]:
    url = f"{wompi_api_base()}{path}"
    try:
        with httpx.Client(timeout=25.0) as client:
            response = client.request(method, url, headers=headers, json=json)
    except httpx.HTTPError as exc:
        log.warning("Wompi %s %s falló: %s", method, path, exc)
        _alert_wompi_down(f"{method} {path}: {type(exc).__name__} {str(exc)[:200]}")
        raise WompiError("No pudimos comunicarnos con Wompi. Intenta de nuevo en unos minutos.") from exc
    if response.status_code >= 400:
        message = _error_message(response)
        log.warning("Wompi %s %s → %s: %s", method, path, response.status_code, message)
        # 4xx de validación (tarjeta, datos) es del cliente; 401/403/5xx es nuestro o de Wompi.
        if response.status_code >= 500 or response.status_code in (401, 403):
            _alert_wompi_down(f"{method} {path} → HTTP {response.status_code}: {message}")
        raise WompiError(message)
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def get_acceptance_tokens() -> dict[str, str]:
    """Tokens prefirmados de Wompi (términos y tratamiento de datos) con sus enlaces."""
    public_key = (settings.wompi_public_key or "").strip()
    try:
        data = _request("GET", "/merchants/info", headers={"x-merchant-public-key": public_key})
    except WompiError:
        # Endpoint anterior; Wompi lo retira el 31 de octubre de 2026.
        data = _request("GET", f"/merchants/{public_key}", headers={})
    acceptance = data.get("presigned_acceptance") or {}
    personal = data.get("presigned_personal_data_auth") or {}
    if not acceptance.get("acceptance_token") or not personal.get("acceptance_token"):
        raise WompiError("Wompi no devolvió los tokens de aceptación.")
    return {
        "acceptance_token": acceptance["acceptance_token"],
        "acceptance_permalink": acceptance.get("permalink") or "",
        "personal_data_token": personal["acceptance_token"],
        "personal_data_permalink": personal.get("permalink") or "",
    }


def create_payment_source(*, source_type: str, token: str, customer_email: str) -> dict[str, Any]:
    tokens = get_acceptance_tokens()
    return _request(
        "POST",
        "/payment_sources",
        headers={"Authorization": f"Bearer {_private_key()}"},
        json={
            "type": source_type,
            "token": token,
            "customer_email": customer_email,
            "acceptance_token": tokens["acceptance_token"],
            "accept_personal_auth": tokens["personal_data_token"],
        },
    )


def void_payment_source(source_id: str) -> None:
    _request(
        "PUT",
        f"/payment_sources/{source_id}/void",
        headers={"Authorization": f"Bearer {_private_key()}"},
    )


def create_source_transaction(
    *,
    reference: str,
    amount_in_cents: int,
    customer_email: str,
    payment_source_id: str,
    source_type: str,
) -> dict[str, Any]:
    """Cobra a una fuente de pago guardada. Wompi casi siempre responde PENDING."""
    tokens = get_acceptance_tokens()
    body: dict[str, Any] = {
        "amount_in_cents": amount_in_cents,
        "currency": "COP",
        "signature": build_integrity_signature(reference, amount_in_cents),
        "customer_email": customer_email,
        "reference": reference,
        "payment_source_id": int(payment_source_id),
        "acceptance_token": tokens["acceptance_token"],
        "accept_personal_auth": tokens["personal_data_token"],
    }
    if source_type == "CARD":
        body["payment_method"] = {"installments": 1}
        # Credential On File: mejora la aprobación de cobros periódicos con Visa/Mastercard.
        body["recurrent"] = True
    return _request(
        "POST",
        "/transactions",
        headers={"Authorization": f"Bearer {_private_key()}"},
        json=body,
    )


def find_transaction_by_reference(reference: str) -> Optional[dict[str, Any]]:
    """Última transacción con esa referencia (para cobros cuya respuesta se perdió)."""
    url = f"{wompi_api_base()}/transactions"
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.get(
                url,
                params={"reference": reference},
                headers={"Authorization": f"Bearer {_private_key()}"},
            )
    except httpx.HTTPError as exc:
        raise WompiError("No pudimos consultar Wompi.") from exc
    if response.status_code >= 400:
        raise WompiError(_error_message(response))
    data = (response.json() or {}).get("data")
    if isinstance(data, list):
        return data[0] if data else None
    return data if isinstance(data, dict) else None


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

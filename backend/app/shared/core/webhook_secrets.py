from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Optional, Union

from app.config import settings


def evolution_webhook_secret_for(tenant_id: Union[uuid.UUID, str]) -> str:
    """Clave del webhook de un negocio, derivada de la maestra.

    Si una instancia de Evolution o su configuración se filtra, solo sirve para ese negocio.
    """
    master = settings.evolution_webhook_secret.encode("utf-8")
    return hmac.new(master, f"evolution-webhook:{tenant_id}".encode(), hashlib.sha256).hexdigest()


def secret_fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def secrets_match(expected: str, provided: Optional[str]) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def evolution_webhook_authorized(tenant_id: Union[uuid.UUID, str], provided: Optional[str]) -> bool:
    if secrets_match(evolution_webhook_secret_for(tenant_id), provided):
        return True
    return settings.evolution_webhook_accept_legacy_secret and secrets_match(
        settings.evolution_webhook_secret, provided
    )

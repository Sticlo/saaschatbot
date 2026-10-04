from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.domain.entities import LegalConsent

# Cambiar junto con web/site/src/app/core/legal.ts cada vez que se publique una versión
# nueva de los documentos (y pedir de nuevo la aceptación si el cambio es sustancial).
TERMS_VERSION = "2026-10-04"
PRIVACY_VERSION = "2026-10-04"

LEGAL_REQUIRED_MESSAGE = (
    "Para crear la cuenta debes aceptar los Términos y Condiciones "
    "y la Política de Tratamiento de Datos Personales."
)


def record_legal_consent(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    email: str,
    method: str,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    marketing_opt_in: bool = False,
) -> LegalConsent:
    consent = LegalConsent(
        tenant_id=tenant_id,
        user_id=user_id,
        email=email.lower().strip(),
        terms_version=TERMS_VERSION,
        privacy_version=PRIVACY_VERSION,
        marketing_opt_in=marketing_opt_in,
        method=method,
        ip_address=(ip_address or None) and ip_address[:45],
        user_agent=(user_agent or None) and user_agent[:400],
    )
    db.add(consent)
    return consent

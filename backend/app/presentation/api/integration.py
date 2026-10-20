"""API para siochat: en qué va cada negocio que prospectó (servidor a servidor, con token)."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.application.integration.siochat import find_tenant_by_phone, normalize_phone, tenant_status
from app.config import settings
from app.infrastructure.persistence.database import get_db

router = APIRouter(prefix="/api/integracion", tags=["integracion"])


def _require_token(request: Request) -> None:
    expected = settings.integration_api_token.strip()
    auth = request.headers.get("authorization") or ""
    presented = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    # Sin token configurado la ruta no existe: no se expone nada por accidente.
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Token inválido", headers={"WWW-Authenticate": "Bearer"})


@router.get("/estado", dependencies=[Depends(_require_token)])
def lead_status(telefono: str = Query(..., max_length=30), db: Session = Depends(get_db)):
    phone = normalize_phone(telefono)
    if phone is None:
        raise HTTPException(status_code=422, detail="Teléfono inválido: usa 57 + 10 dígitos")
    tenant = find_tenant_by_phone(db, phone)
    if tenant is None:
        return {"registrado": False, "telefono": phone}
    return {**tenant_status(db, tenant), "telefono": phone}

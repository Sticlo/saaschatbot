from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.services.queue_service import enqueue_webhook

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/evolution", tags=["webhooks"])


@router.post("/{tenant_id}")
async def evolution_webhook(tenant_id: uuid.UUID, request: Request):
    secret = request.headers.get("X-Webhook-Secret") or request.headers.get("x-webhook-secret")
    if secret != settings.evolution_webhook_secret:
        raise HTTPException(status_code=401, detail="Webhook no autorizado")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON inválido") from exc

    try:
        enqueue_webhook(tenant_id, payload)
    except Exception as exc:
        log.exception("No se pudo encolar webhook tenant=%s", tenant_id)
        raise HTTPException(status_code=503, detail="Cola no disponible") from exc

    return {"received": True, "queued": True}

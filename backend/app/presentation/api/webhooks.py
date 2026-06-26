from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.config import settings
from app.application.workers.queue_service import enqueue_webhook

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/evolution", tags=["webhooks"])


def _normalize_event(payload: dict) -> str:
    return (payload.get("event") or "").lower().replace("_", ".")


@router.post("/{tenant_id}")
async def evolution_webhook(tenant_id: uuid.UUID, request: Request):
    secret = request.headers.get("X-Webhook-Secret") or request.headers.get("x-webhook-secret")
    if secret != settings.evolution_webhook_secret:
        raise HTTPException(status_code=401, detail="Webhook no autorizado")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON inválido") from exc

    event = _normalize_event(payload)
    instance = str(
        payload.get("instance") or payload.get("instanceName") or ""
    )
    msg_id = ""
    data = payload.get("data") or {}
    if isinstance(data, dict):
        key = data.get("key") if isinstance(data.get("key"), dict) else {}
        msg_id = str(key.get("id") or "")

    # Mensajes: procesar al instante (sin cola) para sync tipo WhatsApp Web.
    if event == "messages.upsert":
        from app.application.messaging.webhook_processor import process_evolution_webhook
        from app.application.sync.webhook_trace_service import record_webhook_event

        try:
            await run_in_threadpool(process_evolution_webhook, tenant_id, payload)
            record_webhook_event(
                tenant_id,
                event=event,
                result="processed",
                message_id=msg_id,
                instance=instance,
            )
        except Exception as exc:
            record_webhook_event(
                tenant_id,
                event=event,
                result="error",
                detail=str(exc),
                message_id=msg_id,
                instance=instance,
            )
            log.exception("Error procesando webhook tenant=%s", tenant_id)
            raise HTTPException(status_code=500, detail="Error procesando mensaje") from exc
        return {"received": True, "processed": True}

    try:
        enqueue_webhook(tenant_id, payload)
        from app.application.sync.webhook_trace_service import record_webhook_event

        record_webhook_event(
            tenant_id,
            event=event or "unknown",
            result="queued",
            message_id=msg_id,
            instance=instance,
        )
    except Exception as exc:
        log.exception("No se pudo encolar webhook tenant=%s", tenant_id)
        raise HTTPException(status_code=503, detail="Cola no disponible") from exc

    return {"received": True, "queued": True}

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.infrastructure.persistence.database import SessionLocal
from app.application.chatwoot.chatwoot_webhook_processor import process_chatwoot_webhook

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/chatwoot", tags=["webhooks"])


@router.post("")
async def chatwoot_webhook(request: Request):
    if not settings.chatwoot_enabled:
        return {"received": True, "ignored": True}

    secret = request.headers.get("X-Chatwoot-Secret") or request.headers.get("x-chatwoot-secret")
    if secret and secret != settings.chatwoot_webhook_secret:
        raise HTTPException(status_code=401, detail="Webhook Chatwoot no autorizado")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON inválido") from exc

    db = SessionLocal()
    try:
        process_chatwoot_webhook(db, payload)
    except Exception:
        log.exception("Error procesando webhook Chatwoot")
        raise HTTPException(status_code=500, detail="Error procesando webhook") from exc
    finally:
        db.close()

    return {"received": True}

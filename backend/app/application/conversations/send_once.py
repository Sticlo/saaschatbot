"""Un mensaje del panel sale una sola vez aunque la red se corte y el panel lo reintente.

El panel manda cada mensaje con su propia Idempotency-Key. Si la respuesta se perdió en un
micro-corte (el mensaje sí salió), el reintento recibe el mismo mensaje en vez de enviarlo de nuevo."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, Optional

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.domain.entities import Message
from app.infrastructure.cache.redis_client import get_redis

log = logging.getLogger(__name__)

TTL_SECONDS = 15 * 60
IN_FLIGHT = "sending"
WAIT_SECONDS = 20.0


def _key(tenant_id: uuid.UUID, raw: str) -> str:
    return f"send:once:{tenant_id}:{raw}"


def _text(value) -> Optional[str]:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def _already_sent(db: Session, tenant_id: uuid.UUID, raw: Optional[str]) -> Optional[Message]:
    try:
        message_id = uuid.UUID(raw or "")
    except ValueError:
        return None
    return db.query(Message).filter(Message.id == message_id, Message.tenant_id == tenant_id).first()


def send_once(db: Session, request: Request, tenant_id: uuid.UUID, send: Callable[[], Message]) -> Message:
    raw = (request.headers.get("Idempotency-Key") or "").strip()[:80]
    if not raw:
        return send()
    try:
        client = get_redis()
        key = _key(tenant_id, raw)
        deadline = time.monotonic() + WAIT_SECONDS
        while not client.set(key, IN_FLIGHT, nx=True, ex=TTL_SECONDS):
            current = _text(client.get(key))
            if current and current != IN_FLIGHT:
                sent = _already_sent(db, tenant_id, current)
                if sent is not None:
                    log.info("Reintento del panel: el mensaje ya había salido msg=%s", sent.id)
                    return sent
                client.delete(key)
            if time.monotonic() > deadline:
                raise HTTPException(status_code=409, detail="Ese mensaje todavía se está enviando")
            time.sleep(0.3)
    except HTTPException:
        raise
    except Exception:
        log.warning("send_once sin Redis: se envía sin protección de duplicados", exc_info=True)
        return send()

    try:
        message = send()
    except BaseException:
        try:
            client.delete(key)
        except Exception:
            pass
        raise
    try:
        client.set(key, str(message.id), ex=TTL_SECONDS)
    except Exception:
        log.warning("send_once: no se pudo recordar el envío msg=%s", message.id, exc_info=True)
    return message

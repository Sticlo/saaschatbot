from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.shared.core.auth_cookies import AUTH_COOKIE_NAME
from app.shared.core.ws_auth import authenticate_ws_token, resolve_ws_token, ws_origin_allowed
from app.infrastructure.cache.redis_client import get_redis, tenant_cache_key

log = logging.getLogger(__name__)

router = APIRouter(tags=["panel"])

# Una sesión cerrada o revocada deja de recibir eventos en este plazo como máximo.
SESSION_RECHECK_SECONDS = 120
_PROTOCOL_PREFIX = "bearer."


def _protocol_token(websocket: WebSocket) -> tuple[str, str | None]:
    raw = websocket.headers.get("sec-websocket-protocol") or ""
    for item in (part.strip() for part in raw.split(",")):
        if item.startswith(_PROTOCOL_PREFIX):
            return item[len(_PROTOCOL_PREFIX):], item
    return "", None


@router.websocket("/ws/panel")
async def panel_websocket(websocket: WebSocket):
    if not ws_origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=4403, reason="Origen no permitido")
        return

    protocol_token, protocol = _protocol_token(websocket)
    auth_token = resolve_ws_token(
        cookie_token=websocket.cookies.get(AUTH_COOKIE_NAME, ""),
        protocol_token=protocol_token,
    )
    loop = asyncio.get_running_loop()
    try:
        current = await loop.run_in_executor(None, authenticate_ws_token, auth_token)
    except Exception:
        await websocket.close(code=4401, reason="No autorizado")
        return

    channel = tenant_cache_key(str(current.tenant_id), "events")
    await websocket.accept(subprotocol=protocol)
    pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(channel)
    last_check = time.monotonic()

    try:
        await websocket.send_json({"type": "connected", "tenant_id": str(current.tenant_id)})

        while True:
            if time.monotonic() - last_check >= SESSION_RECHECK_SECONDS:
                try:
                    await loop.run_in_executor(None, authenticate_ws_token, auth_token)
                except Exception:
                    await websocket.close(code=4401, reason="Sesión cerrada")
                    break
                last_check = time.monotonic()

            message = await loop.run_in_executor(None, pubsub.get_message, True, 1.0)
            if not message or message.get("type") != "message":
                continue
            data = message.get("data")
            if not data:
                continue
            try:
                await websocket.send_text(data)
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("WebSocket panel error tenant=%s: %s", current.tenant_id, exc)
    finally:
        try:
            pubsub.unsubscribe(channel)
            pubsub.close()
        except Exception:
            pass

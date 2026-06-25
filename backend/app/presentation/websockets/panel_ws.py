from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.shared.core.auth_cookies import AUTH_COOKIE_NAME
from app.shared.core.ws_auth import authenticate_ws_token, resolve_ws_token
from app.infrastructure.cache.redis_client import get_redis, tenant_cache_key

log = logging.getLogger(__name__)

router = APIRouter(tags=["panel"])


@router.websocket("/ws/panel")
async def panel_websocket(websocket: WebSocket, token: str = ""):
    auth_token = resolve_ws_token(
        query_token=token,
        cookie_token=websocket.cookies.get(AUTH_COOKIE_NAME, ""),
    )
    try:
        current = authenticate_ws_token(auth_token)
    except Exception:
        await websocket.close(code=4401, reason="No autorizado")
        return

    channel = tenant_cache_key(str(current.tenant_id), "events")
    await websocket.accept()
    pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(channel)
    loop = asyncio.get_running_loop()

    try:
        await websocket.send_json({"type": "connected", "tenant_id": str(current.tenant_id)})

        while True:
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

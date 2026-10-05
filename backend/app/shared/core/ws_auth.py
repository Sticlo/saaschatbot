from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, status

from app.config import settings
from app.shared.core.deps import CurrentUser
from app.shared.core.sessions import SessionInvalid, resolve_session
from app.infrastructure.persistence.database import SessionLocal


def authenticate_ws_token(token: str) -> CurrentUser:
    with SessionLocal() as db:
        try:
            session = resolve_session(db, token)
        except SessionInvalid as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc) or "Token inválido"
            ) from exc
        return CurrentUser(session.user)


def resolve_ws_token(*, cookie_token: str, protocol_token: str = "") -> str:
    """Cookie HttpOnly (navegador) o subprotocolo `bearer.<token>` (clientes sin cookie).

    Nunca por query string: las URLs quedan en logs de proxies y en el historial.
    """
    cookie = (cookie_token or "").strip()
    if cookie:
        return cookie
    return (protocol_token or "").strip()


def ws_origin_allowed(origin: Optional[str]) -> bool:
    """Bloquea el secuestro de WebSocket desde otros sitios (CSWSH)."""
    if not origin:
        return True
    return origin.rstrip("/") in settings.trusted_origins()

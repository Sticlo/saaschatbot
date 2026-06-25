from __future__ import annotations

import uuid

from fastapi import HTTPException, status

from app.shared.core.auth_cookies import AUTH_COOKIE_NAME
from app.shared.core.deps import CurrentUser
from app.shared.core.security import decode_access_token
from app.infrastructure.persistence.database import SessionLocal
from app.domain.entities import User


def authenticate_ws_token(token: str) -> CurrentUser:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token requerido")
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
        tenant_id = uuid.UUID(payload["tenant_id"])
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido"
        ) from exc

    with SessionLocal() as db:
        user = (
            db.query(User)
            .filter(User.id == user_id, User.tenant_id == tenant_id, User.is_active.is_(True))
            .first()
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Usuario no encontrado o inactivo",
            )
        return CurrentUser(user)


def resolve_ws_token(*, query_token: str, cookie_token: str) -> str:
    """Cookie HttpOnly tiene prioridad sobre query (legacy)."""
    cookie = (cookie_token or "").strip()
    if cookie:
        return cookie
    return (query_token or "").strip()

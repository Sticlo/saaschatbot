from __future__ import annotations

import uuid

from fastapi import HTTPException, status

from app.core.deps import CurrentUser
from app.core.security import decode_access_token
from app.database import SessionLocal
from app.models import User


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

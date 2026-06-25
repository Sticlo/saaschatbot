from __future__ import annotations

import uuid
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.shared.core.auth_cookies import AUTH_COOKIE_NAME
from app.shared.core.permissions import role_at_least
from app.shared.core.security import decode_access_token
from app.infrastructure.persistence.database import get_db
from app.domain.entities import User, UserRole

bearer_scheme = HTTPBearer(auto_error=False)


class CurrentUser:
    def __init__(self, user: User):
        self.user = user
        self.id = user.id
        self.tenant_id = user.tenant_id
        self.email = user.email
        self.role = user.role
        self.full_name = user.full_name


def _resolve_access_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
) -> str:
    if credentials is not None and credentials.scheme.lower() == "bearer":
        token = (credentials.credentials or "").strip()
        if token:
            return token
    cookie_token = (request.cookies.get(AUTH_COOKIE_NAME) or "").strip()
    if cookie_token:
        return cookie_token
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Autenticación requerida",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    request: Request,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> CurrentUser:
    token = _resolve_access_token(request, credentials)
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
        tenant_id = uuid.UUID(payload["tenant_id"])
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
        ) from exc

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


def require_role(minimum: UserRole):
    def dependency(current: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        if not role_at_least(current.role, minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permisos insuficientes",
            )
        return current

    return dependency


RequireOwner = Annotated[CurrentUser, Depends(require_role(UserRole.OWNER))]
RequireAgent = Annotated[CurrentUser, Depends(require_role(UserRole.AGENT))]
RequireViewer = Annotated[CurrentUser, Depends(require_role(UserRole.VIEWER))]

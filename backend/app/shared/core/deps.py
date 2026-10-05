from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.shared.core.auth_cookies import AUTH_COOKIE_NAME
from app.shared.core.permissions import role_at_least
from app.shared.core.sessions import SessionInvalid, issue_token, needs_refresh, resolve_session
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
        session = resolve_session(db, token)
    except SessionInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc) or "Token inválido",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    request.state.session_payload = session.payload
    from_cookie = token == (request.cookies.get(AUTH_COOKIE_NAME) or "").strip()
    if from_cookie and needs_refresh(session):
        # SessionRefreshMiddleware la pone en la respuesta: el usuario activo no tiene que re-entrar.
        request.state.renewed_token = issue_token(session.user)
    return CurrentUser(session.user)


def require_role(minimum: UserRole):
    def dependency(current: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        if not role_at_least(current.role, minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permisos insuficientes",
            )
        return current

    return dependency


def require_platform_admin(current: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    # 404 y no 403: a un cliente cualquiera ni le contamos que la consola existe.
    if (current.email or "").strip().lower() not in settings.platform_admins():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return current


RequireOwner = Annotated[CurrentUser, Depends(require_role(UserRole.OWNER))]
RequirePlatformAdmin = Annotated[CurrentUser, Depends(require_platform_admin)]
RequireAgent = Annotated[CurrentUser, Depends(require_role(UserRole.AGENT))]
RequireViewer = Annotated[CurrentUser, Depends(require_role(UserRole.VIEWER))]

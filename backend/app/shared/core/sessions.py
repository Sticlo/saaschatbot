from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import Tenant, User
from app.infrastructure.cache.redis_client import get_redis
from app.shared.core.security import create_access_token, decode_access_token

log = logging.getLogger(__name__)

_REVOKED_PREFIX = "auth:revoked:"


class SessionInvalid(Exception):
    """El token no corresponde a una sesión válida (firma, versión, revocado o usuario inactivo)."""


@dataclass
class ResolvedSession:
    user: User
    payload: dict[str, Any]

    @property
    def issued_at(self) -> int:
        return int(self.payload.get("iat") or 0)


def issue_token(user: User) -> str:
    return create_access_token(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        role=user.role,
        email=user.email,
        token_version=user.token_version or 0,
    )


def revoke_token(payload: dict[str, Any]) -> None:
    """Invalida un token concreto (cerrar sesión en este dispositivo) hasta que expire."""
    jti = payload.get("jti")
    if not jti:
        return
    ttl = int(payload.get("exp") or 0) - int(time.time())
    if ttl <= 0:
        return
    try:
        get_redis().set(f"{_REVOKED_PREFIX}{jti}", "1", ex=ttl)
    except Exception:
        log.exception("No se pudo revocar el token %s", jti)


def is_token_revoked(jti: str) -> bool:
    try:
        return bool(get_redis().exists(f"{_REVOKED_PREFIX}{jti}"))
    except Exception:
        # Sin Redis no se puede consultar la lista; la versión en Postgres sigue protegiendo.
        log.warning("Redis no disponible al validar revocación de sesión")
        return False


def revoke_all_sessions(user: User) -> None:
    """Invalida todas las sesiones del usuario (incluida la actual). El llamador hace commit."""
    user.token_version = (user.token_version or 0) + 1


def resolve_session(db: Session, token: str) -> ResolvedSession:
    if not token:
        raise SessionInvalid("Token requerido")
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(str(payload["sub"]))
        tenant_id = uuid.UUID(str(payload["tenant_id"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise SessionInvalid("Token inválido") from exc

    if is_token_revoked(str(payload["jti"])):
        raise SessionInvalid("Sesión cerrada")

    row = (
        db.query(User)
        .join(Tenant, Tenant.id == User.tenant_id)
        .filter(
            User.id == user_id,
            User.tenant_id == tenant_id,
            User.is_active.is_(True),
            Tenant.is_active.is_(True),
        )
        .first()
    )
    if row is None:
        raise SessionInvalid("Usuario no encontrado o inactivo")
    if int(payload.get("ver") or 0) != int(row.token_version or 0):
        raise SessionInvalid("Sesión expirada. Vuelve a entrar.")
    return ResolvedSession(user=row, payload=payload)


def needs_refresh(session: ResolvedSession, *, now: Optional[float] = None) -> bool:
    age = (now or time.time()) - session.issued_at
    return age >= settings.session_refresh_after_minutes * 60

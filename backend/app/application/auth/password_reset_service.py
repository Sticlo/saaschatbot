from __future__ import annotations

import json
import secrets
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.application.billing.tenant_service import log_audit
from app.config import settings
from app.domain.entities import User
from app.infrastructure.cache.redis_client import cache_delete, cache_get, cache_set
from app.infrastructure.email.email_service import send_password_reset_email
from app.shared.core.security import hash_password
from app.shared.core.sessions import revoke_all_sessions

_RESET_PREFIX = "password_reset:"


def _cache_key(token: str) -> str:
    return f"{_RESET_PREFIX}{token}"


def _ttl_seconds() -> int:
    return max(5, int(settings.password_reset_expire_minutes)) * 60


def create_password_reset_token(*, email: str, user_id: str) -> Tuple[str, Optional[str]]:
    token = secrets.token_urlsafe(32)
    payload = {"email": email, "user_id": user_id}
    cache_set(_cache_key(token), json.dumps(payload), _ttl_seconds())
    url = f"{settings.site_public_url.rstrip('/')}/restablecer?token={token}"
    dev_link = send_password_reset_email(to_email=email, url=url)
    return token, dev_link


def reset_password_with_token(db: Session, token: str, new_password: str, *, ip_address: Optional[str]) -> User:
    raw = cache_get(_cache_key(token))
    if not raw:
        raise ValueError("Enlace inválido o expirado")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Enlace inválido") from exc

    email = str(payload.get("email") or "").lower().strip()
    user_id = str(payload.get("user_id") or "").strip()
    if not email or not user_id:
        raise ValueError("Enlace inválido")

    user = (
        db.query(User)
        .filter(User.id == user_id, User.email == email, User.is_active.is_(True))
        .first()
    )
    if user is None:
        raise ValueError("No hay cuenta activa con ese enlace")

    if not user.hashed_password and user.oauth_provider:
        raise ValueError(
            f"Esta cuenta usa {user.oauth_provider.title()}. Entra con ese método."
        )

    # Validar antes de gastar el enlace: una clave débil no obliga a pedir otro correo.
    user.hashed_password = hash_password(new_password, email=user.email)
    cache_delete(_cache_key(token))
    # Si alguien entró a la cuenta, restablecer la clave lo saca de todos los dispositivos.
    revoke_all_sessions(user)
    log_audit(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        action="user.password_reset",
        ip_address=ip_address,
    )
    db.flush()
    return user

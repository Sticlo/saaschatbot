from __future__ import annotations

import json
import secrets
from typing import Any, Optional, Tuple

from sqlalchemy.orm import Session

from app.application.billing.tenant_service import log_audit, register_tenant_with_owner
from app.config import settings
from app.domain.entities import Tenant, User
from app.infrastructure.cache.redis_client import cache_delete, cache_get, cache_set, tenant_cache_key
from app.infrastructure.email.email_service import send_magic_link_email
from app.shared.core.security import hash_password

_MAGIC_PREFIX = "magic_link:"


def _cache_key(token: str) -> str:
    return f"{_MAGIC_PREFIX}{token}"


def _ttl_seconds() -> int:
    return max(5, int(settings.magic_link_expire_minutes)) * 60


def create_magic_link(
    *,
    email: str,
    kind: str,
    business_name: str = "",
    owner_name: str = "",
) -> Tuple[str, Optional[str]]:
    token = secrets.token_urlsafe(32)
    payload = {
        "email": email,
        "kind": kind,
        "business_name": business_name.strip(),
        "owner_name": owner_name.strip(),
    }
    cache_set(_cache_key(token), json.dumps(payload), _ttl_seconds())
    url = f"{settings.site_public_url.rstrip('/')}/auth/entrar?token={token}"
    dev_link = send_magic_link_email(
        to_email=email,
        url=url,
        signup=kind == "register",
    )
    return token, dev_link


def consume_magic_link(db: Session, token: str, *, ip_address: Optional[str]) -> User:
    raw = cache_get(_cache_key(token))
    if not raw:
        raise ValueError("Enlace inválido o expirado")

    try:
        payload: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Enlace inválido") from exc

    email = str(payload.get("email") or "").lower().strip()
    kind = str(payload.get("kind") or "login")
    if not email:
        raise ValueError("Enlace inválido")

    cache_delete(_cache_key(token))

    if kind == "register":
        business_name = str(payload.get("business_name") or "").strip()
        owner_name = str(payload.get("owner_name") or "").strip()
        if len(business_name) < 2 or len(owner_name) < 2:
            raise ValueError("Datos de registro incompletos en el enlace")

        temp_password = secrets.token_urlsafe(18)
        tenant, owner = register_tenant_with_owner(
            db,
            business_name=business_name,
            owner_name=owner_name,
            email=email,
            hashed_password=hash_password(temp_password),
        )
        log_audit(
            db,
            tenant_id=tenant.id,
            user_id=owner.id,
            action="tenant.registered",
            details={"business_name": tenant.business_name, "via": "magic_link"},
            ip_address=ip_address,
        )
        cache_set(tenant_cache_key(str(tenant.id), "plan"), tenant.plan, ttl_seconds=900)
        db.flush()
        return owner

    user = db.query(User).filter(User.email == email, User.is_active.is_(True)).first()
    if user is None:
        raise ValueError("No hay cuenta activa con ese correo")

    tenant = db.query(Tenant).filter(Tenant.id == user.tenant_id, Tenant.is_active.is_(True)).first()
    if tenant is None:
        raise ValueError("Cuenta de negocio inactiva")

    log_audit(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        action="user.login",
        details={"via": "magic_link"},
        ip_address=ip_address,
    )
    db.flush()
    return user

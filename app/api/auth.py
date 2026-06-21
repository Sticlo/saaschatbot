from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import RequireViewer
from app.core.security import create_access_token, hash_password, verify_password
from app.database import get_db
from app.models import Tenant, User
from app.redis_client import cache_set, tenant_cache_key
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.schemas.plans import ChangePasswordRequest
from app.services.tenant_service import log_audit, register_tenant_with_owner
from fastapi import Depends

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_for_user(user: User) -> TokenResponse:
    token = create_access_token(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        role=user.role,
        email=user.email,
    )
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        email=user.email,
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(
    body: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        tenant, owner = register_tenant_with_owner(
            db,
            business_name=body.business_name.strip(),
            owner_name=body.owner_name.strip(),
            email=body.email.lower().strip(),
            hashed_password=hash_password(body.password),
        )
        log_audit(
            db,
            tenant_id=tenant.id,
            user_id=owner.id,
            action="tenant.registered",
            details={"business_name": tenant.business_name, "slug": tenant.slug},
            ip_address=request.client.host if request.client else None,
        )
        db.commit()
        db.refresh(owner)

        cache_set(
            tenant_cache_key(str(tenant.id), "plan"),
            tenant.plan,
            ttl_seconds=900,
        )
        return _token_for_user(owner)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El email ya está registrado en este tenant o slug duplicado",
        ) from exc
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    email = body.email.lower().strip()
    user = db.query(User).filter(User.email == email, User.is_active.is_(True)).first()
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos",
        )

    tenant = db.query(Tenant).filter(Tenant.id == user.tenant_id, Tenant.is_active.is_(True)).first()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cuenta de negocio inactiva",
        )

    user.last_login_at = datetime.now(timezone.utc)
    log_audit(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        action="user.login",
        ip_address=request.client.host if request.client else None,
    )
    db.commit()

    cache_set(tenant_cache_key(str(tenant.id), "plan"), tenant.plan, ttl_seconds=900)
    return _token_for_user(user)


@router.get("/me", response_model=UserResponse)
def me(current: RequireViewer):
    return current.user


@router.patch("/me/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    current: RequireViewer,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == current.id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if not verify_password(body.current_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Contraseña actual incorrecta",
        )
    if body.current_password == body.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La nueva contraseña debe ser diferente",
        )

    user.hashed_password = hash_password(body.new_password)
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="user.password_changed",
        ip_address=request.client.host if request.client else None,
    )
    db.commit()


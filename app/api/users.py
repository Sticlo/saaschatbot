from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import RequireOwner, RequireViewer
from app.core.security import hash_password
from app.database import get_db
from app.models import User, UserRole
from app.schemas.auth import InviteUserRequest, UpdateUserRoleRequest, UserResponse
from app.services.subscription_service import get_tenant_subscription
from app.services.tenant_service import log_audit

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserResponse])
def list_users(current: RequireViewer, db: Session = Depends(get_db)):
    users = (
        db.query(User)
        .filter(User.tenant_id == current.tenant_id)
        .order_by(User.created_at.asc())
        .all()
    )
    return users


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def invite_user(
    body: InviteUserRequest,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    subscription = get_tenant_subscription(db, current.tenant_id)
    if subscription is None or subscription.plan is None:
        raise HTTPException(status_code=404, detail="Suscripción no encontrada")

    active_count = (
        db.query(User)
        .filter(User.tenant_id == current.tenant_id, User.is_active.is_(True))
        .count()
    )
    if active_count >= subscription.plan.max_team_members:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Límite de usuarios alcanzado ({subscription.plan.max_team_members} "
                f"en plan {subscription.plan.name})"
            ),
        )

    user = User(
        tenant_id=current.tenant_id,
        email=body.email.lower().strip(),
        hashed_password=hash_password(body.password),
        full_name=body.full_name.strip(),
        role=body.role,
    )
    db.add(user)
    try:
        log_audit(
            db,
            tenant_id=current.tenant_id,
            user_id=current.id,
            action="user.invited",
            details={"email": user.email, "role": user.role},
            ip_address=request.client.host if request.client else None,
        )
        db.commit()
        db.refresh(user)
        return user
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe un usuario con ese email en este negocio",
        ) from exc


@router.patch("/{user_id}/role", response_model=UserResponse)
def update_user_role(
    user_id: uuid.UUID,
    body: UpdateUserRoleRequest,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    user = (
        db.query(User)
        .filter(User.id == user_id, User.tenant_id == current.tenant_id)
        .first()
    )
    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if user.id == current.id:
        raise HTTPException(
            status_code=400, detail="No puedes cambiar tu propio rol"
        )
    if user.role == UserRole.OWNER.value and body.role != UserRole.OWNER.value:
        owners = (
            db.query(User)
            .filter(
                User.tenant_id == current.tenant_id,
                User.role == UserRole.OWNER.value,
                User.is_active.is_(True),
            )
            .count()
        )
        if owners <= 1:
            raise HTTPException(
                status_code=400,
                detail="Debe existir al menos un owner activo",
            )

    user.role = body.role
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="user.role_updated",
        details={"target_user_id": str(user_id), "new_role": body.role},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_user(
    user_id: uuid.UUID,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    user = (
        db.query(User)
        .filter(User.id == user_id, User.tenant_id == current.tenant_id)
        .first()
    )
    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if user.id == current.id:
        raise HTTPException(status_code=400, detail="No puedes desactivarte a ti mismo")

    user.is_active = False
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="user.deactivated",
        details={"target_user_id": str(user_id)},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()

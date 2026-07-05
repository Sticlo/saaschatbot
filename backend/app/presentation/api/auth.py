from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.shared.core.auth_cookies import clear_auth_cookie, set_auth_cookie

from app.shared.core.deps import RequireViewer
from app.shared.core.security import create_access_token, hash_password, verify_password
from app.infrastructure.persistence.database import get_db
from app.domain.entities import Tenant, User
from app.infrastructure.cache.redis_client import cache_set, tenant_cache_key
from app.presentation.schemas.auth import (
    EmailLookupRequest,
    EmailLookupResponse,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    MagicLinkRequest,
    MagicLinkResponse,
    MagicLinkVerifyRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserResponse,
)
from app.application.auth.password_reset_service import (
    create_password_reset_token,
    reset_password_with_token,
)
from app.application.auth.oauth_service import (
    consume_oauth_finish_token,
    consume_oauth_state,
    create_oauth_finish_token,
    create_oauth_state,
    fetch_github_profile,
    fetch_google_profile,
    github_authorize_url,
    google_authorize_url,
    oauth_provider_enabled,
    resolve_oauth_user,
)
from app.presentation.schemas.plans import ChangePasswordRequest
from app.application.billing.tenant_service import log_audit, register_tenant_with_owner
from app.application.auth.magic_link_service import consume_magic_link, create_magic_link
from app.application.billing.subscription_service import get_tenant_subscription
from app.config import settings
from app.domain.entities.enums import SubscriptionStatus
from app.shared.core.rate_limit import rate_limit_exceeded, rate_limit_record

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger(__name__)

_FORGOT_PASSWORD_MESSAGE = (
    "Si el correo tiene una cuenta con contraseña, te enviamos un enlace para restablecerla."
)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _auth_json_response(token_body: TokenResponse, *, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(content=token_body.model_dump(mode="json"), status_code=status_code)
    set_auth_cookie(response, token_body.access_token)
    return response


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
        pwd_hash = hash_password(body.password)
        tenant, owner = register_tenant_with_owner(
            db,
            business_name=body.business_name.strip(),
            owner_name=body.owner_name.strip(),
            email=body.email.lower().strip(),
            hashed_password=pwd_hash,
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
        return _auth_json_response(_token_for_user(owner), status_code=status.HTTP_201_CREATED)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
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


@router.post("/lookup-email", response_model=EmailLookupResponse)
def lookup_email(body: EmailLookupRequest, db: Session = Depends(get_db)):
    email = body.email.lower().strip()
    exists = (
        db.query(User.id)
        .filter(User.email == email, User.is_active.is_(True))
        .first()
        is not None
    )
    return EmailLookupResponse(email=email, exists=exists)


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
def forgot_password(body: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
    email = body.email.lower().strip()
    ip = _client_ip(request)

    if rate_limit_exceeded(
        f"forgot:email:{email}",
        limit=settings.auth_forgot_max_attempts,
    ) or rate_limit_exceeded(
        f"forgot:ip:{ip}",
        limit=settings.auth_forgot_max_attempts * 3,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demasiados intentos. Espera unos minutos e intenta de nuevo.",
        )

    rate_limit_record(f"forgot:email:{email}", window_seconds=settings.auth_forgot_window_seconds)
    rate_limit_record(f"forgot:ip:{ip}", window_seconds=settings.auth_forgot_window_seconds)

    dev_link: Optional[str] = None
    user = db.query(User).filter(User.email == email, User.is_active.is_(True)).first()
    if user and user.hashed_password:
        try:
            _, dev_link = create_password_reset_token(
                email=email,
                user_id=str(user.id),
            )
        except RuntimeError as exc:
            log.exception("Forgot password email failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    return ForgotPasswordResponse(message=_FORGOT_PASSWORD_MESSAGE, dev_link=dev_link)


@router.post("/reset-password", response_model=TokenResponse)
def reset_password(body: ResetPasswordRequest, request: Request, db: Session = Depends(get_db)):
    ip = _client_ip(request)
    if rate_limit_exceeded(f"reset:ip:{ip}", limit=20):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demasiados intentos. Espera unos minutos e intenta de nuevo.",
        )
    rate_limit_record(f"reset:ip:{ip}", window_seconds=3600)

    try:
        user = reset_password_with_token(
            db,
            body.token.strip(),
            body.password,
            ip_address=ip,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)

    tenant = db.query(Tenant).filter(Tenant.id == user.tenant_id).first()
    if tenant:
        cache_set(tenant_cache_key(str(tenant.id), "plan"), tenant.plan, ttl_seconds=900)

    return _auth_json_response(_token_for_user(user))


@router.get("/providers")
def auth_providers():
    return {
        "google": oauth_provider_enabled("google"),
        "github": oauth_provider_enabled("github"),
    }


def _is_safe_next_path(path: str) -> bool:
    cleaned = (path or "").strip()
    return cleaned.startswith("/") and not cleaned.startswith("//")


def _site_url(path: str) -> str:
    site = settings.site_public_url.rstrip("/")
    api = settings.app_public_url.rstrip("/")
    if path.startswith("http://") or path.startswith("https://"):
        if path.startswith(site) or path.startswith(api):
            return path
        return site
    if path.startswith("/api/"):
        return f"{api}{path}"
    if path.startswith("/panel"):
        return f"{site}{path}"
    return f"{site}{path}"


def _resolve_oauth_redirect(user: User, db: Session, next_url: Optional[str]) -> str:
    if next_url and _is_safe_next_path(next_url):
        return _site_url(next_url)

    subscription = get_tenant_subscription(db, user.tenant_id)
    if subscription and subscription.status == SubscriptionStatus.TRIAL.value:
        return _site_url("/panel?welcome=1")

    return settings.oauth_success_redirect


def _oauth_error_redirect(message: str) -> RedirectResponse:
    from urllib.parse import quote

    base = settings.oauth_error_redirect.rstrip("/")
    return RedirectResponse(f"{base}?error={quote(message)}")


def _with_oauth_flag(url: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}oauth=1"


def _to_site_relative(url: str) -> str:
    base = settings.site_public_url.rstrip("/")
    if url.startswith(base):
        suffix = url[len(base):]
        return suffix or "/"
    if _is_safe_next_path(url):
        return url
    return "/precios"


def _oauth_finish_url(access_token: str, destination: str) -> str:
    from urllib.parse import urlencode

    finish_token = create_oauth_finish_token(access_token)
    site = settings.site_public_url.rstrip("/")
    params = urlencode({"token": finish_token, "next": _to_site_relative(destination)})
    return f"{site}/api/v1/auth/oauth/finish?{params}"


def _oauth_success_redirect(user: User, db: Session, next_url: Optional[str] = None) -> RedirectResponse:
    access_token = create_access_token(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        role=user.role,
        email=user.email,
    )
    destination = _resolve_oauth_redirect(user, db, next_url)
    return RedirectResponse(_oauth_finish_url(access_token, destination))


@router.get("/oauth/finish")
def oauth_finish(
    token: str = Query(..., min_length=16),
    next: Optional[str] = Query(default=None),
):
    """Sets session cookie on the site origin after OAuth callback on the API port."""
    access_token = consume_oauth_finish_token(token.strip())
    if not access_token:
        return _oauth_error_redirect("Enlace de inicio expirado. Intenta de nuevo.")

    if next and _is_safe_next_path(next):
        target = _with_oauth_flag(_site_url(next))
    else:
        target = _with_oauth_flag(settings.oauth_success_redirect)

    response = RedirectResponse(target)
    set_auth_cookie(response, access_token)
    return response


@router.get("/google/start")
def google_oauth_start(next: Optional[str] = Query(default=None)):
    if not oauth_provider_enabled("google"):
        return _oauth_error_redirect(
            "Google no está configurado. Añade GOOGLE_CLIENT_ID al .env del backend."
        )
    state = create_oauth_state("google", next)
    return RedirectResponse(google_authorize_url(state))


@router.get("/google/callback")
def google_oauth_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
):
    if error:
        return _oauth_error_redirect("Cancelaste el inicio con Google")
    state_ok, next_url = consume_oauth_state(state or "", "google")
    if not code or not state_ok:
        return _oauth_error_redirect("Enlace de Google inválido o expirado")
    try:
        profile = fetch_google_profile(code)
        user = resolve_oauth_user(
            db,
            profile,
            ip_address=request.client.host if request.client else None,
        )
        user.last_login_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user)
        return _oauth_success_redirect(user, db, next_url)
    except ValueError as exc:
        db.rollback()
        return _oauth_error_redirect(str(exc))
    except Exception:
        db.rollback()
        log.exception("Google OAuth callback failed")
        return _oauth_error_redirect("No pudimos entrar con Google")


@router.get("/github/start")
def github_oauth_start(next: Optional[str] = Query(default=None)):
    if not oauth_provider_enabled("github"):
        return _oauth_error_redirect(
            "GitHub no está configurado. Añade GITHUB_CLIENT_ID al .env del backend."
        )
    state = create_oauth_state("github", next)
    return RedirectResponse(github_authorize_url(state))


@router.get("/github/callback")
def github_oauth_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
):
    if error:
        return _oauth_error_redirect("Cancelaste el inicio con GitHub")
    state_ok, next_url = consume_oauth_state(state or "", "github")
    if not code or not state_ok:
        return _oauth_error_redirect("Enlace de GitHub inválido o expirado")
    try:
        profile = fetch_github_profile(code)
        user = resolve_oauth_user(
            db,
            profile,
            ip_address=request.client.host if request.client else None,
        )
        user.last_login_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user)
        return _oauth_success_redirect(user, db, next_url)
    except ValueError as exc:
        db.rollback()
        return _oauth_error_redirect(str(exc))
    except Exception:
        db.rollback()
        log.exception("GitHub OAuth callback failed")
        return _oauth_error_redirect("No pudimos entrar con GitHub")


@router.post("/magic-link", response_model=MagicLinkResponse)
def request_magic_link(body: MagicLinkRequest, db: Session = Depends(get_db)):
    email = body.email.lower().strip()
    user = (
        db.query(User.id)
        .filter(User.email == email, User.is_active.is_(True))
        .first()
    )

    if user is None:
        business_name = (body.business_name or "").strip()
        owner_name = (body.owner_name or "").strip()
        if len(business_name) < 2 or len(owner_name) < 2:
            return MagicLinkResponse(
                sent=False,
                needs_signup=True,
                message="Completa los datos de tu negocio para crear la cuenta.",
            )
        _, dev_link = create_magic_link(
            email=email,
            kind="register",
            business_name=business_name,
            owner_name=owner_name,
        )
        return MagicLinkResponse(
            sent=True,
            message="Te enviamos un enlace para crear tu cuenta.",
            dev_link=dev_link,
        )

    _, dev_link = create_magic_link(email=email, kind="login")
    return MagicLinkResponse(
        sent=True,
        message="Te enviamos un enlace para entrar.",
        dev_link=dev_link,
    )


@router.post("/magic-link/verify", response_model=TokenResponse)
def verify_magic_link(body: MagicLinkVerifyRequest, request: Request, db: Session = Depends(get_db)):
    try:
        user = consume_magic_link(
            db,
            body.token.strip(),
            ip_address=request.client.host if request.client else None,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    tenant = db.query(Tenant).filter(Tenant.id == user.tenant_id).first()
    if tenant:
        cache_set(tenant_cache_key(str(tenant.id), "plan"), tenant.plan, ttl_seconds=900)

    return _auth_json_response(_token_for_user(user))


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    email = body.email.lower().strip()
    ip = _client_ip(request)
    rate_key = f"login:{email}:{ip}"

    if rate_limit_exceeded(rate_key, limit=settings.auth_login_max_attempts):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demasiados intentos. Espera unos minutos e intenta de nuevo.",
        )

    user = db.query(User).filter(User.email == email, User.is_active.is_(True)).first()
    if user is None:
        rate_limit_record(rate_key, window_seconds=settings.auth_login_window_seconds)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos",
        )
    if not user.hashed_password:
        provider = (user.oauth_provider or "Google/GitHub").title()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Esta cuenta usa {provider}. Entra con ese botón.",
        )
    if not verify_password(body.password, user.hashed_password):
        rate_limit_record(rate_key, window_seconds=settings.auth_login_window_seconds)
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
    return _auth_json_response(_token_for_user(user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    clear_auth_cookie(response)


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
    if not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta usa Google o GitHub. No tiene contraseña local.",
        )
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

    try:
        user.hashed_password = hash_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="user.password_changed",
        ip_address=request.client.host if request.client else None,
    )
    db.commit()


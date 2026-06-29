from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.application.billing.tenant_service import log_audit, register_tenant_with_owner
from app.config import settings
from app.domain.entities import Tenant, User
from app.infrastructure.cache.redis_client import cache_delete, cache_get, cache_set, tenant_cache_key

log = logging.getLogger(__name__)

_OAUTH_STATE_PREFIX = "oauth_state:"
_OAUTH_STATE_TTL = 600

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"


@dataclass
class OAuthProfile:
    provider: str
    subject: str
    email: str
    full_name: str


def oauth_provider_enabled(provider: str) -> bool:
    if provider == "google":
        return bool((settings.google_client_id or "").strip())
    if provider == "github":
        return bool((settings.github_client_id or "").strip())
    return False


def create_oauth_state(provider: str, next_url: Optional[str] = None) -> str:
    state = secrets.token_urlsafe(24)
    payload = provider if not next_url else f"{provider}|{next_url}"
    cache_set(f"{_OAUTH_STATE_PREFIX}{state}", payload, _OAUTH_STATE_TTL)
    return state


def consume_oauth_state(state: str, provider: str) -> tuple[bool, Optional[str]]:
    key = f"{_OAUTH_STATE_PREFIX}{state}"
    stored = cache_get(key)
    cache_delete(key)
    if not stored:
        return False, None
    if "|" in stored:
        stored_provider, next_url = stored.split("|", 1)
    else:
        stored_provider, next_url = stored, None
    if stored_provider != provider:
        return False, None
    return True, (next_url.strip() or None) if next_url else None


_OAUTH_FINISH_PREFIX = "oauth_finish:"
_OAUTH_FINISH_TTL = 120


def create_oauth_finish_token(access_token: str) -> str:
    token = secrets.token_urlsafe(32)
    cache_set(f"{_OAUTH_FINISH_PREFIX}{token}", access_token, _OAUTH_FINISH_TTL)
    return token


def consume_oauth_finish_token(token: str) -> Optional[str]:
    key = f"{_OAUTH_FINISH_PREFIX}{token}"
    access_token = cache_get(key)
    cache_delete(key)
    return access_token or None


def google_authorize_url(state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def github_authorize_url(state: str) -> str:
    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": settings.github_redirect_uri,
        "scope": "read:user user:email",
        "state": state,
    }
    return f"{GITHUB_AUTH_URL}?{urlencode(params)}"


def fetch_google_profile(code: str) -> OAuthProfile:
    with httpx.Client(timeout=20.0) as client:
        token_res = client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        if token_res.status_code >= 400:
            log.error("Google token error %s: %s", token_res.status_code, token_res.text)
            raise ValueError("No pudimos validar tu cuenta de Google")

        access_token = token_res.json().get("access_token")
        if not access_token:
            raise ValueError("Google no devolvió un token válido")

        user_res = client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_res.status_code >= 400:
            raise ValueError("No pudimos leer tu perfil de Google")

        data = user_res.json()
        email = str(data.get("email") or "").lower().strip()
        subject = str(data.get("sub") or "").strip()
        name = str(data.get("name") or email.split("@")[0] or "Usuario").strip()
        if not email or not subject:
            raise ValueError("Google no compartió email verificado")

        return OAuthProfile(provider="google", subject=subject, email=email, full_name=name)


def fetch_github_profile(code: str) -> OAuthProfile:
    with httpx.Client(timeout=20.0) as client:
        token_res = client.post(
            GITHUB_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "redirect_uri": settings.github_redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        if token_res.status_code >= 400:
            log.error("GitHub token error %s: %s", token_res.status_code, token_res.text)
            raise ValueError("No pudimos validar tu cuenta de GitHub")

        access_token = token_res.json().get("access_token")
        if not access_token:
            raise ValueError("GitHub no devolvió un token válido")

        auth_header = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        user_res = client.get(GITHUB_USER_URL, headers=auth_header)
        if user_res.status_code >= 400:
            raise ValueError("No pudimos leer tu perfil de GitHub")

        user_data = user_res.json()
        subject = str(user_data.get("id") or "").strip()
        name = str(user_data.get("name") or user_data.get("login") or "Usuario").strip()
        email = str(user_data.get("email") or "").lower().strip()

        if not email:
            emails_res = client.get(GITHUB_EMAILS_URL, headers=auth_header)
            if emails_res.status_code < 400:
                for row in emails_res.json():
                    if row.get("primary") and row.get("verified"):
                        email = str(row.get("email") or "").lower().strip()
                        break
                if not email:
                    for row in emails_res.json():
                        if row.get("verified") and row.get("email"):
                            email = str(row["email"]).lower().strip()
                            break

        if not subject or not email:
            raise ValueError("GitHub no compartió un email verificado")

        return OAuthProfile(provider="github", subject=subject, email=email, full_name=name)


def resolve_oauth_user(
    db: Session,
    profile: OAuthProfile,
    *,
    ip_address: Optional[str],
) -> User:
    by_oauth = (
        db.query(User)
        .filter(
            User.oauth_provider == profile.provider,
            User.oauth_subject == profile.subject,
            User.is_active.is_(True),
        )
        .first()
    )
    if by_oauth:
        return _finalize_login(db, by_oauth, profile.provider, ip_address)

    by_email = (
        db.query(User)
        .filter(User.email == profile.email, User.is_active.is_(True))
        .first()
    )
    if by_email:
        if by_email.oauth_provider and by_email.oauth_provider != profile.provider:
            raise ValueError(
                f"Este email ya está vinculado con {by_email.oauth_provider.title()}. "
                f"Entra con ese método."
            )
        by_email.oauth_provider = profile.provider
        by_email.oauth_subject = profile.subject
        if profile.full_name and not by_email.full_name:
            by_email.full_name = profile.full_name
        db.flush()
        return _finalize_login(db, by_email, profile.provider, ip_address)

    business_name = f"Negocio de {profile.full_name.split()[0] if profile.full_name else 'Omitel'}"
    tenant, owner = register_tenant_with_owner(
        db,
        business_name=business_name,
        owner_name=profile.full_name,
        email=profile.email,
        hashed_password=None,
    )
    owner.oauth_provider = profile.provider
    owner.oauth_subject = profile.subject
    log_audit(
        db,
        tenant_id=tenant.id,
        user_id=owner.id,
        action="tenant.registered",
        details={"business_name": tenant.business_name, "via": profile.provider},
        ip_address=ip_address,
    )
    cache_set(tenant_cache_key(str(tenant.id), "plan"), tenant.plan, ttl_seconds=900)
    db.flush()
    return _finalize_login(db, owner, profile.provider, ip_address)


def _finalize_login(
    db: Session,
    user: User,
    provider: str,
    ip_address: Optional[str],
) -> User:
    tenant = db.query(Tenant).filter(Tenant.id == user.tenant_id, Tenant.is_active.is_(True)).first()
    if tenant is None:
        raise ValueError("Cuenta de negocio inactiva")

    log_audit(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        action="user.login",
        details={"via": provider},
        ip_address=ip_address,
    )
    cache_set(tenant_cache_key(str(tenant.id), "plan"), tenant.plan, ttl_seconds=900)
    db.flush()
    return user

from __future__ import annotations

import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
import jwt

from app.config import settings

BCRYPT_ROUNDS = 12
# bcrypt solo usa los primeros 72 bytes (y bcrypt>=5 rechaza claves más largas).
BCRYPT_MAX_BYTES = 72
TOKEN_ISSUER = "omitel"
TOKEN_TYPE_ACCESS = "access"

_COMMON_PASSWORDS = frozenset(
    {
        "12345678", "123456789", "1234567890", "password", "password1", "contraseña",
        "contrasena", "qwertyui", "qwerty123", "11111111", "00000000", "abcd1234",
        "iloveyou", "colombia", "omitel123", "admin123", "12341234", "87654321",
    }
)

# Hash fijo para gastar el mismo tiempo cuando el correo no existe (evita enumerar cuentas).
_DUMMY_HASH = bcrypt.hashpw(b"omitel-timing-equalizer", bcrypt.gensalt(rounds=BCRYPT_ROUNDS))


def validate_password_policy(password: str, *, email: Optional[str] = None) -> None:
    cleaned = (password or "").strip()
    if len(cleaned) < 8:
        raise ValueError("La contraseña debe tener al menos 8 caracteres")
    if len((password or "").encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValueError("La contraseña es demasiado larga (máximo 72 caracteres)")
    if cleaned.lower() in _COMMON_PASSWORDS or len(set(cleaned)) < 4:
        raise ValueError("Esa contraseña es muy fácil de adivinar. Usa una más única.")
    if email and cleaned.lower() == email.strip().lower():
        raise ValueError("La contraseña no puede ser tu correo")


def hash_password(password: str, *, email: Optional[str] = None) -> str:
    validate_password_policy(password, email=email)
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(plain: str, hashed: Optional[str]) -> bool:
    candidate = (plain or "").encode("utf-8")[:BCRYPT_MAX_BYTES]
    if not hashed:
        bcrypt.checkpw(candidate, _DUMMY_HASH)
        return False
    try:
        return bcrypt.checkpw(candidate, hashed.encode())
    except ValueError:
        return False


def burn_password_check_time(plain: str) -> None:
    """Llamar cuando el usuario no existe: el login tarda lo mismo que con una clave errada."""
    bcrypt.checkpw((plain or "").encode("utf-8")[:BCRYPT_MAX_BYTES], _DUMMY_HASH)


def create_access_token(
    *,
    user_id: str,
    tenant_id: str,
    role: str,
    email: str,
    token_version: int = 0,
    expires_delta: Optional[timedelta] = None,
) -> str:
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=settings.jwt_access_token_expire_minutes))
    payload: Dict[str, Any] = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "role": role,
        "email": email,
        "ver": int(token_version or 0),
        "jti": secrets.token_urlsafe(16),
        "typ": TOKEN_TYPE_ACCESS,
        "iss": TOKEN_ISSUER,
        "iat": now,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            issuer=TOKEN_ISSUER,
            options={"require": ["exp", "iat", "sub", "jti", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise ValueError("Token inválido o expirado") from exc
    if payload.get("typ") != TOKEN_TYPE_ACCESS:
        raise ValueError("Token inválido o expirado")
    return payload


def slugify(value: str) -> str:
    normalized = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return slug or "negocio"

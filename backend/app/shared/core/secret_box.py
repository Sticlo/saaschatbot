"""Cifra en la base los tokens de terceros (p. ej. el permiso que un dueño le da a Omitel en su web)."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _box() -> Fernet:
    digest = hashlib.sha256(f"omitel-secret-box:{settings.secret_key}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _box().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _box().decrypt(value.encode()).decode()
    except InvalidToken:
        return ""

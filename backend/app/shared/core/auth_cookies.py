from __future__ import annotations

from fastapi import Response

from app.config import settings

AUTH_COOKIE_NAME = "saaschatbot_session"


def set_auth_cookie(response: Response, token: str) -> None:
    secure = settings.app_env.lower() in ("production", "prod")
    max_age = settings.jwt_access_token_expire_minutes * 60
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key=AUTH_COOKIE_NAME, path="/")

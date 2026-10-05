"""Middlewares ASGI de seguridad (puros: no usan BaseHTTPMiddleware y soportan streaming)."""

from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

import anyio
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response

from app.config import settings
from app.shared.core.auth_cookies import AUTH_COOKIE_NAME, set_auth_cookie
from app.shared.core.rate_limit import client_ip_from, is_rate_limited

log = logging.getLogger(__name__)

Scope = dict
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Llamadas servidor a servidor (Evolution, Wompi, Chatwoot): no traen Origin ni cookie de sesión.
_MACHINE_PREFIXES = ("/webhooks/", "/api/v1/billing/wompi/webhook")
_UPLOAD_PREFIXES = ("/api/v1/outbound/assets", "/api/v1/quick-shortcuts/files")
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


async def _send_json(send: Send, status: int, detail: str, extra_headers: dict[str, str] | None = None) -> None:
    body = json.dumps({"detail": detail}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    for key, value in (extra_headers or {}).items():
        headers.append((key.lower().encode(), value.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _is_machine_path(path: str) -> bool:
    return path.startswith(_MACHINE_PREFIXES)


class SecurityHeadersMiddleware:
    """Cabeceras defensivas en todas las respuestas HTTP (además de las que ponga Caddy)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault(
                    "Permissions-Policy",
                    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
                )
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
                if settings.is_production():
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"
                    )
                if not path.startswith("/static/") and path != "/panel":
                    headers.setdefault("Content-Security-Policy", _API_CSP)
                if path.startswith(("/api/", "/webhooks/")):
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodySizeLimitMiddleware:
    """Corta cuerpos gigantes antes de leerlos en memoria (DoS por payload)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _limit_for(path: str) -> int:
        if path.startswith("/webhooks/"):
            return settings.max_webhook_body_bytes
        if path.startswith(_UPLOAD_PREFIXES):
            return settings.max_upload_body_bytes
        return settings.max_request_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(scope.get("path", ""))
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await _send_json(send, 413, "El contenido enviado es demasiado grande")
                    return
            except ValueError:
                await _send_json(send, 400, "Content-Length inválido")
                return

        received = 0
        response_started = False

        async def limited_receive() -> dict:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not response_started:
                await _send_json(send, 413, "El contenido enviado es demasiado grande")


class _BodyTooLarge(Exception):
    pass


class RateLimitMiddleware:
    """Límite por IP y minuto para toda la API: frena floods y fuerza bruta distribuida en rutas."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET")
        if path.startswith("/static/") or method == "OPTIONS" or path in ("/health", "/"):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        ip = client_ip_from(Headers(scope=scope), client[0] if client else None)
        if _is_machine_path(path):
            bucket, limit = "hook", settings.rate_limit_webhook_per_minute
        elif path.startswith("/api/v1/auth/") and method in _UNSAFE_METHODS:
            bucket, limit = "auth", settings.rate_limit_auth_per_minute
        else:
            bucket, limit = "api", settings.rate_limit_api_per_minute

        try:
            limited = await anyio.to_thread.run_sync(
                lambda: is_rate_limited(f"mw:{bucket}:{ip}", limit=limit, window_seconds=60)
            )
        except Exception:
            limited = False
        if limited:
            log.warning("Rate limit %s ip=%s path=%s", bucket, ip, path)
            await _send_json(
                send,
                429,
                "Demasiadas solicitudes. Espera un momento e intenta de nuevo.",
                {"Retry-After": "60"},
            )
            return
        await self.app(scope, receive, send)


class OriginGuardMiddleware:
    """CSRF: si un navegador envía la cookie de sesión desde otro sitio, se rechaza.

    SameSite=Lax ya bloquea la mayoría de casos; esto cubre navegadores viejos y subdominios.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        headers = Headers(scope=scope)
        if _is_machine_path(path) or AUTH_COOKIE_NAME not in (headers.get("cookie") or ""):
            await self.app(scope, receive, send)
            return
        origin = (headers.get("origin") or "").rstrip("/")
        if origin and origin != "null" and origin not in settings.trusted_origins():
            host = headers.get("host") or ""
            scheme = scope.get("scheme", "http")
            if origin != f"{scheme}://{host}":
                log.warning("Origen rechazado %s en %s", origin, path)
                await _send_json(send, 403, "Origen no permitido")
                return
        await self.app(scope, receive, send)


class SessionRefreshMiddleware:
    """Renueva la cookie de sesión cuando get_current_user lo pide (sesión deslizante)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                state = scope.get("state")
                token = getattr(state, "renewed_token", None) if state is not None else None
                if not token and isinstance(state, dict):
                    token = state.get("renewed_token")
                headers = MutableHeaders(scope=message)
                already_sets_cookie = any(
                    AUTH_COOKIE_NAME in value for value in headers.getlist("set-cookie")
                )
                if token and not already_sets_cookie and message.get("status", 500) < 400:
                    holder = Response()
                    set_auth_cookie(holder, token)
                    for key, value in holder.raw_headers:
                        if key == b"set-cookie":
                            headers.append("set-cookie", value.decode("latin-1"))
            await send(message)

        await self.app(scope, receive, send_wrapper)

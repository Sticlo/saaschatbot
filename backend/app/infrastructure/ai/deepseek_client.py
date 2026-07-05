from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import ALLOWED_DEEPSEEK_MODEL, settings

log = logging.getLogger(__name__)

_PLACEHOLDER_API_KEYS = frozenset(
    {
        "",
        "change-me",
        "changeme",
        "<tu-api-key-deepseek>",
        "your-api-key",
        "sk-xxx",
    }
)


class DeepSeekError(Exception):
    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def is_configured() -> bool:
    key = settings.deepseek_api_key.strip()
    if not key or key.lower() in _PLACEHOLDER_API_KEYS:
        return False
    if key.startswith("<") and key.endswith(">"):
        return False
    return True


def _friendly_http_error(status_code: int, detail: str) -> str:
    if status_code == 401:
        return "API key DeepSeek inválida — revisa DEEPSEEK_API_KEY en .env"
    if status_code == 402 or "Insufficient Balance" in detail:
        return "Sin saldo en DeepSeek — recarga en platform.deepseek.com"
    if status_code == 403:
        return (
            "DeepSeek rechazó la petición (403). Verifica que la API key sea válida "
            "y tenga acceso en platform.deepseek.com"
        )
    if status_code == 429:
        return "DeepSeek limitó las peticiones (429) — espera un momento e intenta de nuevo"
    return f"DeepSeek HTTP {status_code}: {detail[:200]}"


def check_provider_health() -> tuple[bool, Optional[str]]:
    """Ping mínimo a DeepSeek. Retorna (ok, error)."""
    if not is_configured():
        return False, "Configura DEEPSEEK_API_KEY en tu archivo .env"
    try:
        chat_completion(
            [{"role": "user", "content": "Responde solo: ok"}],
            max_tokens=5,
            timeout=20.0,
        )
        return True, None
    except DeepSeekError as exc:
        return False, str(exc)[:240]


def resolve_model(model: Optional[str] = None) -> str:
    """Siempre deepseek-chat — bloquea reasoner, v4-pro, etc."""
    requested = (model or settings.deepseek_chat_model or ALLOWED_DEEPSEEK_MODEL).strip().lower()
    if requested != ALLOWED_DEEPSEEK_MODEL:
        log.warning("Modelo DeepSeek %r ignorado — usando %s", requested, ALLOWED_DEEPSEEK_MODEL)
        return ALLOWED_DEEPSEEK_MODEL
    return ALLOWED_DEEPSEEK_MODEL


def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    timeout: float = 60.0,
) -> str:
    if not is_configured():
        raise DeepSeekError("DEEPSEEK_API_KEY no configurada — agrégala en .env")

    effective_model = resolve_model(model)
    payload: dict[str, Any] = {
        "model": effective_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key.strip()}",
        "Content-Type": "application/json",
    }
    url = f"{settings.deepseek_api_base.rstrip('/')}/chat/completions"

    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.post(url, json=payload, headers=headers)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        detail = exc.response.text[:500] if exc.response is not None else str(exc)
        raise DeepSeekError(
            _friendly_http_error(status, detail),
            status_code=status or None,
        ) from exc
    except httpx.HTTPError as exc:
        msg = str(exc).strip()
        raise DeepSeekError(
            "No se pudo conectar con DeepSeek. Confirma que el backend esté corriendo "
            f"(./scripts/dev.sh) y que haya internet. Detalle: {msg[:180]}"
        ) from exc

    if response.status_code >= 400:
        detail = response.text[:500]
        raise DeepSeekError(
            _friendly_http_error(response.status_code, detail),
            status_code=response.status_code,
        )

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise DeepSeekError("DeepSeek devolvió respuesta vacía")

    content = choices[0].get("message", {}).get("content")
    if not content or not str(content).strip():
        raise DeepSeekError("DeepSeek devolvió contenido vacío")
    return str(content).strip()

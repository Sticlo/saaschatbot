from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import ALLOWED_DEEPSEEK_MODEL, settings

log = logging.getLogger(__name__)


class DeepSeekError(Exception):
    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def is_configured() -> bool:
    return bool(settings.deepseek_api_key.strip())


def check_provider_health() -> tuple[bool, Optional[str]]:
    """Ping mínimo a DeepSeek. Retorna (ok, error)."""
    if not is_configured():
        return False, "API key no configurada"
    try:
        chat_completion(
            [{"role": "user", "content": "Responde solo: ok"}],
            max_tokens=5,
            timeout=20.0,
        )
        return True, None
    except DeepSeekError as exc:
        msg = str(exc)
        if "402" in msg or "Insufficient Balance" in msg:
            return False, "Sin saldo en DeepSeek — recarga en platform.deepseek.com"
        if "401" in msg:
            return False, "API key DeepSeek inválida"
        return False, msg[:200]


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
        raise DeepSeekError("DEEPSEEK_API_KEY no configurada")

    effective_model = resolve_model(model)
    payload: dict[str, Any] = {
        "model": effective_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{settings.deepseek_api_base.rstrip('/')}/chat/completions"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise DeepSeekError(f"Error de red DeepSeek: {exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:500]
        raise DeepSeekError(
            f"DeepSeek HTTP {response.status_code}: {detail}",
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

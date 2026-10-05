"""Texto IA resiliente: DeepSeek, un reintento si el fallo es pasajero y luego Gemini de respaldo.

El cliente final no se entera de qué proveedor respondió; el dev recibe aviso si DeepSeek falla.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from app.config import settings
from app.infrastructure.ai import deepseek_client, gemini_client
from app.infrastructure.ai.deepseek_client import DeepSeekError

log = logging.getLogger(__name__)

RETRY_DELAY_SECONDS = 1.5
FLAKY_WINDOW_SECONDS = 300
FLAKY_THRESHOLD = 3
# Reintentar no arregla saldo, key ni una petición mal formada.
_NON_RETRYABLE_STATUS = frozenset({400, 401, 402, 403, 404, 422})


class AiUnavailableError(DeepSeekError):
    """Ni DeepSeek ni el respaldo respondieron."""


def is_configured() -> bool:
    return deepseek_client.is_configured() or gemini_client.is_configured()


def _is_retryable(exc: DeepSeekError) -> bool:
    return exc.status_code not in _NON_RETRYABLE_STATUS


def _report_deepseek_failure(exc: DeepSeekError) -> None:
    from app.application.monitoring.dev_alerts import alert_dev, count_in_window

    fallback = "Las respuestas siguen saliendo por Gemini." if gemini_client.is_configured() else (
        "No hay GEMINI_API_KEY de respaldo: los chats quedan en espera."
    )
    if not _is_retryable(exc):
        alert_dev(
            "ai:deepseek:rejected",
            "DeepSeek rechaza las peticiones (saldo, API key o petición inválida)",
            f"{exc}\n{fallback}",
            severity="critical",
            throttle_seconds=1800,
        )
        return
    if count_in_window("ai:deepseek:fail", FLAKY_WINDOW_SECONDS) >= FLAKY_THRESHOLD:
        alert_dev(
            "ai:deepseek:flaky",
            "DeepSeek está fallando seguido",
            f"Último error: {exc}\nLos clientes no lo notan. {fallback}",
            severity="warning",
        )


def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    timeout: Optional[float] = None,
) -> str:
    effective_timeout = timeout or settings.ai_provider_timeout_seconds
    errors: list[str] = []

    if deepseek_client.is_configured():
        for attempt in (1, 2):
            try:
                return deepseek_client.chat_completion(
                    messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=effective_timeout,
                )
            except DeepSeekError as exc:
                log.warning("DeepSeek falló (intento %s): %s", attempt, exc)
                if attempt == 1 and _is_retryable(exc):
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                errors.append(f"DeepSeek: {exc}")
                _report_deepseek_failure(exc)
                break

    if gemini_client.is_configured():
        try:
            text = gemini_client.chat_text(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=effective_timeout,
            )
            log.info("Respuesta IA servida por Gemini (respaldo)")
            return text
        except gemini_client.GeminiError as exc:
            log.warning("Gemini (respaldo) falló: %s", exc)
            errors.append(f"Gemini: {exc}")

    from app.application.monitoring.dev_alerts import alert_dev

    detail = "\n".join(errors) or "Ningún proveedor IA configurado (DEEPSEEK_API_KEY / GEMINI_API_KEY)"
    alert_dev(
        "ai:all_down",
        "La IA no responde: DeepSeek y Gemini fallaron",
        f"{detail}\nLos chats quedan en espera con reintentos automáticos y luego pasan al dueño.",
        severity="critical",
        throttle_seconds=600,
    )
    raise AiUnavailableError(detail)

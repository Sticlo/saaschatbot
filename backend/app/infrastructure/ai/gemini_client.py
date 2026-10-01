from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)

INAUDIBLE = "[inaudible]"
RECEIPT_PREFIX = "Comprobante de pago"

_TRANSCRIBE_PROMPT = (
    "Transcribe literalmente esta nota de voz de un cliente que escribe a un negocio por WhatsApp. "
    "Respeta el idioma original. Devuelve solo la transcripción, sin comillas, títulos ni comentarios. "
    f"Si no hay voz entendible, responde exactamente: {INAUDIBLE}"
)

_DESCRIBE_IMAGE_PROMPT = (
    "Un cliente envió esta imagen a un negocio por WhatsApp. Descríbela en español para que el "
    "asistente de ventas sepa qué mandó y pueda responderle. Máximo 3 frases, sin títulos ni comillas.\n"
    "- Copia el texto importante que se vea (precios, nombres, fechas, direcciones).\n"
    "- Si es un producto, habitación, plato o lugar, di qué es y sus rasgos visibles.\n"
    f"- Si es un comprobante de pago o transferencia (Nequi, Daviplata, Bancolombia, PSE, etc.), empieza con "
    f"'{RECEIPT_PREFIX}:' y luego monto, fecha, hora, referencia, app o banco y destinatario, si se ven.\n"
    "- No inventes datos que no se lean en la imagen."
)


class GeminiError(Exception):
    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def is_configured() -> bool:
    key = settings.gemini_api_key.strip()
    return bool(key) and not (key.startswith("<") and key.endswith(">"))


def _friendly_http_error(status_code: int, detail: str, model: str) -> str:
    if status_code in (401, 403):
        return "API key de Gemini inválida — revisa GEMINI_API_KEY en .env"
    if status_code == 404:
        return f"Modelo Gemini no disponible ({model}) — revisa GEMINI_AUDIO_MODEL / GEMINI_IMAGE_MODEL"
    if status_code == 429:
        return "Gemini limitó las peticiones (429) — cuota agotada o demasiadas peticiones"
    return f"Gemini HTTP {status_code}: {detail[:200]}"


def _generate_from_media(*, model: str, prompt: str, media_b64: str, mime: str, timeout: float) -> str:
    if not is_configured():
        raise GeminiError("GEMINI_API_KEY no configurada — agrégala en .env")

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime, "data": media_b64}},
                ]
            }
        ]
    }
    url = f"{settings.gemini_api_base.rstrip('/')}/models/{model}:generateContent"
    headers = {"x-goog-api-key": settings.gemini_api_key.strip(), "Content-Type": "application/json"}

    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise GeminiError(f"No se pudo conectar con Gemini: {str(exc)[:180]}") from exc

    if response.status_code >= 400:
        raise GeminiError(
            _friendly_http_error(response.status_code, response.text[:500], model),
            status_code=response.status_code,
        )

    candidates = response.json().get("candidates") or []
    parts = (candidates[0].get("content") or {}).get("parts") or [] if candidates else []
    text = " ".join(
        str(p.get("text", "")).strip() for p in parts if p.get("text") and not p.get("thought")
    ).strip()
    if not text:
        raise GeminiError("Gemini devolvió una respuesta vacía")
    return text


def _base_mime(mimetype: str, default: str) -> str:
    # WhatsApp manda "audio/ogg; codecs=opus"; Gemini solo acepta el tipo base.
    return (mimetype or default).split(";")[0].strip().lower() or default


def transcribe_audio(audio_b64: str, mimetype: str, *, timeout: float = 60.0) -> str:
    """Devuelve la transcripción, o "" si el audio no tiene voz entendible."""
    text = _generate_from_media(
        model=settings.gemini_audio_model,
        prompt=_TRANSCRIBE_PROMPT,
        media_b64=audio_b64,
        mime=_base_mime(mimetype, "audio/ogg"),
        timeout=timeout,
    )
    if text.lower() == INAUDIBLE:
        return ""
    return text


def describe_image(image_b64: str, mimetype: str, *, timeout: float = 60.0) -> str:
    return _generate_from_media(
        model=settings.gemini_image_model,
        prompt=_DESCRIBE_IMAGE_PROMPT,
        media_b64=image_b64,
        mime=_base_mime(mimetype, "image/jpeg"),
        timeout=timeout,
    )

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

_CATALOG_PROMPT = (
    "Este archivo es el catálogo, menú, carta o lista de precios de un negocio. Extrae en español "
    "todo lo que un vendedor necesita para responder clientes por WhatsApp: productos o servicios, "
    "precios, tallas, colores, presentaciones, promociones, condiciones, horarios y contacto.\n"
    "- Una línea por producto: «Nombre — precio — detalles».\n"
    "- Copia los precios tal cual aparecen; no inventes nada que no se lea.\n"
    "- Texto plano, sin markdown ni títulos decorativos. Máximo 3500 caracteres.\n"
    "- Si no se lee información útil, responde exactamente: SIN_INFORMACION"
)
NO_CATALOG_INFO = "SIN_INFORMACION"


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
    return _post_generate(model=model, payload=payload, timeout=timeout)


def _post_generate(*, model: str, payload: dict, timeout: float) -> str:
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


def chat_text(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    timeout: float = 30.0,
) -> str:
    """Misma entrada que DeepSeek (system/user/assistant): respaldo cuando DeepSeek no responde."""
    if not is_configured():
        raise GeminiError("GEMINI_API_KEY no configurada — agrégala en .env")

    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system" and m.get("content"))
    contents: list[dict] = []
    for m in messages:
        role = m.get("role")
        text = (m.get("content") or "").strip()
        if role == "system" or not text:
            continue
        gemini_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == gemini_role:
            contents[-1]["parts"][0]["text"] += f"\n{text}"
        else:
            contents.append({"role": gemini_role, "parts": [{"text": text}]})
    if not contents or contents[0]["role"] != "user":
        contents.insert(0, {"role": "user", "parts": [{"text": "(inicio de la conversación)"}]})

    payload: dict = {
        "contents": contents,
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max(256, max_tokens * 2)},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    return _post_generate(model=settings.gemini_text_model, payload=payload, timeout=timeout)


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


def extract_catalog(file_b64: str, mimetype: str, *, timeout: float = 90.0) -> str:
    """Lee un PDF o foto de catálogo/menú. Devuelve "" si no hay información útil."""
    text = _generate_from_media(
        model=settings.gemini_image_model,
        prompt=_CATALOG_PROMPT,
        media_b64=file_b64,
        mime=_base_mime(mimetype, "application/pdf"),
        timeout=timeout,
    )
    if text.strip().upper().startswith(NO_CATALOG_INFO):
        return ""
    return text.strip()[:4000]


def describe_image(image_b64: str, mimetype: str, *, timeout: float = 60.0) -> str:
    return _generate_from_media(
        model=settings.gemini_image_model,
        prompt=_DESCRIBE_IMAGE_PROMPT,
        media_b64=image_b64,
        mime=_base_mime(mimetype, "image/jpeg"),
        timeout=timeout,
    )

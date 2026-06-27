from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import settings
from app.infrastructure.ai.deepseek_client import DeepSeekError, chat_completion, is_configured

log = logging.getLogger(__name__)

VALID_CATEGORIES = frozenset({"interesado", "duda", "no_interesado", "opt_out", "ruido"})

CLASSIFIER_SYSTEM = """Eres un clasificador de mensajes de WhatsApp para prospección comercial en Colombia.
Responde SOLO con JSON válido, sin markdown, con esta forma exacta:
{"category":"interesado|duda|no_interesado|opt_out|ruido","reason":"breve"}

Categorías:
- interesado: quiere saber más, acepta conversación, tono positivo, saludos informales ("hola", "qué haces", "cómo vas"), charla amistosa
- duda: pregunta precio, horario, cómo funciona, pide info
- no_interesado: rechazo educado, no le interesa, ocupado
- opt_out: pide que no le escriban más, spam, bloqueo, insultos graves
- ruido: SOLO ack mínimo sin conversación ("ok", "k", "👍", emoji suelto) o mensaje vacío

IMPORTANTE: "qué haces", "cómo vas", "hermanito", "te quiero" y saludos informales NO son ruido — son interesado.
"""


def _heuristic_classify(text: str) -> dict[str, Any]:
    lower = text.lower().strip()
    opt_out_patterns = (
        "no me escrib",
        "deja de escrib",
        "no molest",
        "quita mi numero",
        "saca mi numero",
        "spam",
        "bloque",
        "report",
    )
    if any(p in lower for p in opt_out_patterns):
        return {"category": "opt_out", "reason": "heuristic opt_out"}

    if lower in {"ok", "k", "👍", "🙏", "👌"}:
        return {"category": "ruido", "reason": "heuristic ruido"}

    if lower in {"si", "sí", "ya", "hola", "buenas", "buenos dias", "buenas tardes", "buenas noches"}:
        return {"category": "interesado", "reason": "heuristic saludo"}

    no_interest = ("no gracias", "no estoy interesad", "no me interesa", "paso", "no por ahora")
    if any(p in lower for p in no_interest):
        return {"category": "no_interesado", "reason": "heuristic no_interesado"}

    if "?" in text or any(w in lower for w in ("precio", "cuanto", "cómo", "como", "info", "horario")):
        return {"category": "duda", "reason": "heuristic duda"}

    return {"category": "interesado", "reason": "heuristic default"}


def _needs_llm_classifier(text: str, heuristic: dict[str, Any]) -> bool:
    """Solo casos ambiguos justifican gastar tokens en clasificador."""
    if heuristic["category"] in ("opt_out", "ruido", "no_interesado"):
        return False
    if heuristic["reason"].startswith("heuristic saludo"):
        return False
    if heuristic["reason"].startswith("heuristic duda"):
        return False
    # Mensajes cortos: heurística basta
    if len(text.strip()) <= 80:
        return False
    return True


def _is_trivial_noise(text: str) -> bool:
    lower = text.lower().strip()
    if not lower:
        return True
    if lower in {"ok", "k", "👍", "🙏", "👌", "si", "sí", "ya"}:
        return True
    if len(lower) <= 2 and "?" not in text:
        return True
    return False


def _adjust_classification(text: str, result: dict[str, Any]) -> dict[str, Any]:
    """Evita que saludos informales se clasifiquen como ruido y queden sin respuesta."""
    if result.get("category") != "ruido":
        return result
    if _is_trivial_noise(text):
        return result

    heuristic = _heuristic_classify(text)
    if heuristic["category"] != "ruido":
        return {
            **heuristic,
            "reason": f"override ruido→{heuristic['category']}: {heuristic['reason']}",
        }
    return {
        "category": "interesado",
        "reason": "override ruido: mensaje conversacional",
    }


def _parse_classifier_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[^{}]+\}", text)
        if not match:
            raise ValueError("JSON inválido")
        data = json.loads(match.group())

    category = str(data.get("category") or "").strip().lower()
    if category not in VALID_CATEGORIES:
        raise ValueError(f"categoría inválida: {category}")
    return {
        "category": category,
        "reason": str(data.get("reason") or "")[:200],
    }


def _classify_with_llm(text: str, *, business_name: str = "") -> dict[str, Any]:
    raw = chat_completion(
        [
            {"role": "system", "content": CLASSIFIER_SYSTEM},
            {
                "role": "user",
                "content": f"Negocio: {business_name or 'sin nombre'}\nMensaje del contacto:\n{text}",
            },
        ],
        model=settings.deepseek_classifier_model,
        temperature=0.1,
        max_tokens=80,
    )
    return _adjust_classification(text, _parse_classifier_json(raw))


def classify_inbound_message(text: str, *, business_name: str = "") -> dict[str, Any]:
    """Clasifica mensajes. Por defecto usa reglas locales (0 costo); LLM solo si está activado."""
    cleaned = (text or "").strip()
    if not cleaned:
        return {"category": "ruido", "reason": "empty"}

    heuristic = _heuristic_classify(cleaned)

    use_llm = settings.ai_classifier_use_llm and is_configured()
    if use_llm and _needs_llm_classifier(cleaned, heuristic):
        try:
            return _classify_with_llm(cleaned, business_name=business_name)
        except (DeepSeekError, ValueError) as exc:
            log.warning("Clasificador DeepSeek fallback: %s", exc)

    return heuristic

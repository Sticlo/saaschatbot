from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from app.application.ai.ai_conversation_service import build_system_prompt
from app.domain.entities import Tenant, TenantProfile
from app.infrastructure.ai.deepseek_client import DeepSeekError, chat_completion, is_configured

log = logging.getLogger(__name__)

_GENERATOR_SYSTEM = """Eres un copywriter de WhatsApp para negocios en Colombia.
Genera un primer mensaje de contacto corto, cálido y claro para enviar por WhatsApp.
Responde SOLO JSON válido sin markdown:
{
  "text": "saludo en español colombiano",
  "buttons": [{"label": "texto corto del botón", "value": "lo que el cliente diría al tocarlo"}]
}
Reglas:
- Máximo 3 botones; label ≤ 25 caracteres
- Texto del saludo ≤ 450 caracteres, tono natural (tú)
- Los botones son opciones que el cliente puede tocar (ej. Ver menú → Quiero ver el menú)
- No inventes precios ni promesas no mencionadas en el contexto
- Si no hay botones útiles, devuelve buttons: []
"""


def generate_bait_template(
    *,
    tenant: Tenant,
    profile: Optional[TenantProfile],
    tone: str = "",
) -> dict[str, Any]:
    if not is_configured():
        raise DeepSeekError("DeepSeek no configurado")

    context = build_system_prompt(tenant, profile)
    user_parts = [f"Negocio: {tenant.business_name}", f"Contexto:\n{context}"]
    if tone.strip():
        user_parts.append(f"Tono deseado: {tone.strip()}")

    messages = [
        {"role": "system", "content": _GENERATOR_SYSTEM},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    raw = chat_completion(messages, temperature=0.75, max_tokens=600)
    return _parse_generated(raw)


def _parse_generated(raw: str) -> dict[str, Any]:
    text = raw.strip()
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("AI bait JSON inválido: %s", raw[:200])
        raise DeepSeekError("La IA no devolvió JSON válido") from exc

    buttons = data.get("buttons") or []
    if not isinstance(buttons, list):
        buttons = []
    cleaned_buttons = []
    for btn in buttons[:3]:
        if not isinstance(btn, dict):
            continue
        label = str(btn.get("label") or "").strip()
        if not label:
            continue
        value = str(btn.get("value") or label).strip()
        cleaned_buttons.append({"label": label[:40], "value": value[:120]})

    body = str(data.get("text") or "").strip()
    if not body:
        raise DeepSeekError("La IA no generó texto de carnada")

    return {
        "text": body[:2000],
        "button_title": str(data.get("button_title") or "").strip()[:120] or None,
        "button_footer": str(data.get("button_footer") or "").strip()[:255] or None,
        "buttons": cleaned_buttons,
    }

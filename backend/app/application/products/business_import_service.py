"""Del mismo PDF del catálogo, lo que la IA necesita saber además de los productos: condiciones,
cobertura, horarios, pagos y promociones. Solo suma a lo que el dueño ya tiene; nunca borra."""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

from app.application.ai.ai_business_profile_service import build_ai_system_prompt_from_answers
from app.application.billing.tenant_profile_service import answers_from_profile, apply_business_answers
from app.domain.entities import TenantProfile

log = logging.getLogger(__name__)

BUSINESS_LABELS = {
    "restrictions": "Condiciones y políticas",
    "delivery_info": "Cobertura y envíos",
    "location_hours": "Dirección y horario",
    "payment_methods": "Medios de pago",
    "promotions": "Promociones",
}
FIELD_MAX_CHARS = 3000
PROFILE_FIELD_MAX = 10000
# Van solo dentro del prompt del negocio; pagos y promociones tienen su propio bloque.
_PROMPT_FIELDS = ("restrictions", "delivery_info", "location_hours")
_EMPTY = {"", "-", "n/a", "ninguno", "ninguna", "no aparece", "no especificado"}


def clean_business(raw: Any) -> dict[str, str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    found: dict[str, str] = {}
    for key in BUSINESS_LABELS:
        value = raw.get(key)
        if isinstance(value, list):
            value = "\n".join(str(v) for v in value)
        if not isinstance(value, str):
            continue
        lines = [" ".join(line.split()) for line in value.splitlines()]
        text = "\n".join(line for line in lines if line.strip(" -•").lower() not in _EMPTY).strip()
        if text:
            found[key] = text[:FIELD_MAX_CHARS]
    return found


def read_business_info(data: bytes, mime: str) -> dict[str, str]:
    from app.infrastructure.ai.gemini_client import GeminiError, extract_business_info

    try:
        return clean_business(extract_business_info(base64.b64encode(data).decode(), mime))
    except GeminiError as exc:
        log.warning("No se pudo leer la información del negocio del archivo: %s", exc)
        return {}


def _already_has(current: str, text: str) -> bool:
    plain = " ".join(current.lower().split())
    return " ".join(text.lower().split()) in plain


def merge_business(profile: TenantProfile, found: dict[str, str], *, business_name: str) -> list[str]:
    """Devuelve los campos que cambiaron. Los medios de pago solo se llenan si estaban vacíos."""
    answers = answers_from_profile(profile)
    built_before = build_ai_system_prompt_from_answers(business_name=business_name, answers=answers)
    current_prompt = (profile.ai_system_prompt or "").strip()
    custom_prompt = bool(current_prompt) and current_prompt != built_before.strip()

    changed: dict[str, str] = {}
    added: dict[str, str] = {}
    for key, text in clean_business(found).items():
        current = answers.get(key, "")
        if key == "payment_methods" and current:
            continue
        if current and _already_has(current, text):
            continue
        merged = f"{current}\n\n{text}" if current else text
        if len(merged) > PROFILE_FIELD_MAX:
            continue
        changed[key] = merged
        added[key] = text
    if not changed:
        return []
    apply_business_answers(profile, changed)
    answers.update(changed)
    if custom_prompt:
        extra = "".join(
            f"\n\n{BUSINESS_LABELS[key]} (del catálogo):\n{added[key]}" for key in _PROMPT_FIELDS if key in added
        )
        profile.ai_system_prompt = current_prompt + extra
    else:
        profile.ai_system_prompt = build_ai_system_prompt_from_answers(business_name=business_name, answers=answers)
    return list(changed)

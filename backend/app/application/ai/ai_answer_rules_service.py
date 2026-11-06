"""Respuestas oficiales del negocio (cientos): en cada mensaje solo van al prompt las que tienen
que ver con lo que el cliente pregunta. Todas juntas saturan a la IA y responde peor."""
from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Any, Optional, Sequence

from sqlalchemy.orm import Session

from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.domain.entities import Message
from app.domain.entities.enums import MessageDirection

MAX_RULES = 500
MAX_NAME = 200
MAX_TRIGGERS = 1500
MAX_RESPONSE = 1500
MAX_MATERIAL = 120
PROMPT_RULES = 10
CONTEXT_TURNS = 4

_STOPWORDS = {
    "que", "para", "con", "los", "las", "del", "una", "uno", "unos", "unas", "por", "como", "cual", "cuales",
    "este", "esta", "ese", "esa", "eso", "esto", "hay", "tiene", "tienen", "son", "sus", "mas", "pero", "muy",
    "the", "and", "ustedes", "usted", "puedo", "puede", "pueden", "quiero", "seria", "sera", "algo", "todo",
    "toda", "todos", "todas", "hola", "buenas", "buenos", "dias", "tardes", "noches", "gracias", "favor",
}


class AnswerRulesError(ValueError):
    """Mensaje listo para mostrarle al dueño."""


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _stems(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", _plain(text))
    return {w[:5] for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _triggers(raw: Any) -> str:
    if isinstance(raw, (list, tuple)):
        raw = ", ".join(str(item) for item in raw)
    return _clean(raw, MAX_TRIGGERS)


def normalize_rules(raw: Any) -> list[dict]:
    """Acepta la lista o {"rules": [...]}; cada regla con nombre, disparadores y respuesta."""
    if isinstance(raw, dict):
        raw = raw.get("rules") or raw.get("reglas") or []
    if not isinstance(raw, list):
        raise AnswerRulesError("El archivo debe traer una lista de reglas.")
    rules: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("enabled", True)).strip().lower() in {"false", "0", "no"}:
            continue
        response = _clean(item.get("response") or item.get("respuesta"), MAX_RESPONSE)
        name = _clean(item.get("name") or item.get("nombre") or item.get("pregunta"), MAX_NAME)
        triggers = _triggers(item.get("triggers") or item.get("disparadores"))
        if not response or not (name or triggers):
            continue
        mode = str(item.get("mode") or "").strip().lower()
        rules.append(
            {
                "name": name,
                "triggers": triggers,
                "response": response,
                "review": mode in {"review", "revisar", "confirmar"} or bool(item.get("review")),
                "material": _clean(item.get("materialId") or item.get("material") or item.get("portafolio"), MAX_MATERIAL),
            }
        )
        if len(rules) >= MAX_RULES:
            break
    if not rules:
        raise AnswerRulesError("No encontré reglas con pregunta y respuesta en el archivo.")
    return rules


def get_rules(db: Session, tenant_id: uuid.UUID) -> list[dict]:
    profile = get_or_create_tenant_profile(db, tenant_id)
    return list(profile.ai_answer_rules or [])


def save_rules(db: Session, tenant_id: uuid.UUID, rules: list[dict]) -> list[dict]:
    profile = get_or_create_tenant_profile(db, tenant_id)
    profile.ai_answer_rules = rules or None
    db.flush()
    return rules


def _material_stems(material: str) -> set[str]:
    words = [w for w in re.findall(r"[a-z]+", _plain(material)) if len(w) >= 4]
    return {w[:5] for w in words[:2]}


def _score(rule: dict, asked: set[str], context: set[str]) -> float:
    best = 0.0
    for phrase in [rule.get("name") or "", *str(rule.get("triggers") or "").split(",")]:
        stems = _stems(phrase)
        if not stems:
            continue
        hits = len(stems & asked)
        if hits:
            best = max(best, hits / len(stems) + 0.15 * hits)
    if best and _material_stems(rule.get("material") or "") & (asked | context):
        best += 0.3
    return best


def relevant_rules(rules: Sequence[dict], asked_text: str, context_text: str = "", limit: int = PROMPT_RULES) -> list[dict]:
    asked = _stems(asked_text)
    if not asked or not rules:
        return []
    context = _stems(context_text)
    scored = [(score, i, rule) for i, rule in enumerate(rules) if (score := _score(rule, asked, context)) >= 0.5]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [rule for _, _, rule in scored[:limit]]


def _turns(history: Sequence[Message]) -> tuple[str, str]:
    asked: list[str] = []
    rest = list(history)
    while rest and rest[-1].direction == MessageDirection.IN.value:
        asked.insert(0, rest.pop().body or "")
    context = " ".join((m.body or "") for m in rest[-CONTEXT_TURNS:])
    return " ".join(asked), context


def answer_rules_block(rules: Optional[Sequence[dict]], history: Sequence[Message]) -> str:
    if not rules or not history:
        return ""
    asked, context = _turns(history)
    chosen = relevant_rules(rules, asked, context)
    if not chosen:
        return ""
    lines = []
    for rule in chosen:
        where = f" [{rule['material']}]" if rule.get("material") else ""
        check = " (CONFIRMAR: da esta respuesta y pon el tema en missing para que el asesor lo valide)" if rule.get("review") else ""
        lines.append(f"- {rule.get('name') or rule.get('triggers', '')[:80]}{where}: {rule['response']}{check}")
    return (
        "\n\nRespuestas oficiales del negocio que pueden servir para lo que pregunta el cliente ahora. Si una "
        "aplica, úsala con tus palabras, sin cambiar datos ni agregar condiciones; si es de otro portafolio o "
        "no aplica, ignórala. Mandan sobre lo que supongas:\n" + "\n".join(lines)
    )

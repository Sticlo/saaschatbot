"""Qué flujo del negocio pide el cliente, por intención y no por palabras: «¿cómo se separa?»,
«¿cómo contratamos?» y «quiero reservar» son lo mismo. Va aparte del prompt principal porque
ahí, entre catálogo, reglas e historial, la IA a veces pasa por alto el flujo que tocaba."""
from __future__ import annotations

import json
import logging
from typing import Optional, Sequence

from app.domain.entities import Message
from app.domain.entities.enums import MessageDirection
from app.infrastructure.ai.ai_text_provider import chat_completion

log = logging.getLogger(__name__)

CONTEXT_MESSAGES = 6
MESSAGE_CHARS = 300

ROUTER_PROMPT = """Clasificas mensajes de clientes de un negocio por WhatsApp.
Decide si lo que el cliente pide en su último mensaje es lo mismo que alguno de estos temas, por la INTENCIÓN
y no por las palabras exactas (ej.: reservar = separar = apartar = contratar = agendar = «qué hago para tenerlo»;
precios = cuánto vale = qué cuesta = tarifas).
Temas:
{topics}
Reglas:
- Solo escoge un tema si el cliente claramente lo está pidiendo ahora.
- Si encaja con varios temas parecidos, usa la conversación (el paquete o servicio del que venían hablando)
  para escoger. Si aun así no se sabe cuál, responde null.
- Saludos, gracias, «ok», charla o preguntas de otra cosa: null.
Responde solo JSON: {{"flow_id": "<id>"}} o {{"flow_id": null}}"""


def _context(history: Sequence[Message]) -> str:
    lines = []
    for msg in list(history)[-CONTEXT_MESSAGES:]:
        text = " ".join(((getattr(msg, "transcript", None) or msg.body or "")).split())[:MESSAGE_CHARS]
        if text:
            who = "Cliente" if msg.direction == MessageDirection.IN.value else "Negocio"
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


def parse_route(raw: str, valid_ids: frozenset[str]) -> Optional[str]:
    text = (raw or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        flow_id = json.loads(text).get("flow_id")
    except (ValueError, AttributeError):
        return None
    flow_id = str(flow_id or "").strip()
    return flow_id if flow_id in valid_ids else None


def route_flow(topics: Sequence[tuple[str, str]], history: Sequence[Message]) -> Optional[str]:
    """El id del flujo que pide el cliente, o None. Si la clasificación falla, decide la IA principal."""
    if not topics or not history or history[-1].direction != MessageDirection.IN.value:
        return None
    listed = "\n".join(f'- id="{flow_id}": {topic}' for flow_id, topic in topics)
    messages = [
        {"role": "system", "content": ROUTER_PROMPT.format(topics=listed)},
        {"role": "user", "content": _context(history)},
    ]
    try:
        raw = chat_completion(messages, temperature=0.0, max_tokens=40)
    except Exception as exc:
        log.warning("Enrutador de flujos sin respuesta: %s", exc)
        return None
    return parse_route(raw, frozenset(flow_id for flow_id, _ in topics))

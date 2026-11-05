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
- Primero piensa qué está pidiendo de verdad el cliente en su último mensaje, con tus palabras.
- Solo escoge un tema si el cliente claramente lo está pidiendo ahora.
- No confundas etapas: decir que le interesa o que eligió una opción NO es preguntar cómo reservar o
  pagar. Un tema de reservar, separar, abonar o pagar solo aplica si pregunta cómo hacerlo o dice que
  quiere hacerlo ya.
- Si encaja con varios temas parecidos, usa la conversación (el paquete o servicio del que venían hablando)
  para escoger. Si aun así no se sabe cuál, flow_id null.
- Saludos, gracias, «ok», charla o preguntas de otra cosa: flow_id null.
- "seguro" es true solo si no hay ninguna duda razonable de que ese tema es lo que pide.
Responde solo JSON, en este orden:
{{"pide": "<qué pide el cliente, en pocas palabras>", "flow_id": "<id>" o null, "seguro": true o false}}"""


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
        data = json.loads(text)
        flow_id = data.get("flow_id")
    except (ValueError, AttributeError):
        return None
    if data.get("seguro") is not True:
        return None
    flow_id = str(flow_id or "").strip()
    if flow_id in valid_ids:
        log.info("Enrutador: «%s» -> %s", str(data.get("pide") or "")[:120], flow_id)
        return flow_id
    return None


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
        raw = chat_completion(messages, temperature=0.0, max_tokens=160)
    except Exception as exc:
        log.warning("Enrutador de flujos sin respuesta: %s", exc)
        return None
    return parse_route(raw, frozenset(flow_id for flow_id, _ in topics))

"""Flujos del negocio: «cuando el cliente… envía, en orden: mensaje, atajo, atajo».

El saludo se envía solo en el primer mensaje; «pregunta por…» lo reconoce la IA. Los pasos salen
exactos y en orden (la IA no los reescribe) y cada flujo va una sola vez por chat."""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.infrastructure.cache.redis_client import get_redis

log = logging.getLogger(__name__)

TRIGGER_FIRST_CONTACT = "first_contact"
TRIGGER_ASKS_ABOUT = "asks_about"
MAX_FLOWS = 20
MAX_STEPS = 8
MAX_TEXT_CHARS = 2000
MAX_TOPIC_CHARS = 200
FLOW_SENT_TTL_SECONDS = 90 * 24 * 3600


class FlowError(ValueError):
    """Mensaje listo para mostrarle al dueño."""


def _clean(text: Any, limit: int) -> str:
    return str(text or "").strip()[:limit]


def normalize_flows(raw: list[Any], shortcut_ids: set[str]) -> list[dict]:
    out: list[dict] = []
    has_greeting = False
    for item in raw[:MAX_FLOWS]:
        if not isinstance(item, dict):
            continue
        trigger = item.get("trigger")
        if trigger not in (TRIGGER_FIRST_CONTACT, TRIGGER_ASKS_ABOUT):
            continue
        topic = " ".join(_clean(item.get("topic"), MAX_TOPIC_CHARS).split())
        steps = []
        for step in (item.get("steps") or [])[:MAX_STEPS]:
            if not isinstance(step, dict):
                continue
            if step.get("type") == "text" and _clean(step.get("text"), MAX_TEXT_CHARS):
                steps.append({"type": "text", "text": _clean(step.get("text"), MAX_TEXT_CHARS)})
            elif step.get("type") == "shortcut" and str(step.get("shortcut_id") or "") in shortcut_ids:
                steps.append({"type": "shortcut", "shortcut_id": str(step["shortcut_id"])})
        name = " ".join(_clean(item.get("name"), 60).split())
        if not steps:
            raise FlowError(f"El flujo «{name or topic or 'sin nombre'}» no tiene nada para enviar.")
        if trigger == TRIGGER_ASKS_ABOUT and not topic:
            raise FlowError(f"En el flujo «{name or 'sin nombre'}» escribe por qué tiene que preguntar el cliente.")
        active = item.get("active") is not False
        if trigger == TRIGGER_FIRST_CONTACT and active:
            if has_greeting:
                raise FlowError("Solo puede haber un saludo activo.")
            has_greeting = True
        out.append({
            "id": str(item.get("id") or uuid.uuid4()),
            "name": name or ("Bienvenida" if trigger == TRIGGER_FIRST_CONTACT else topic[:60]),
            "trigger": trigger,
            "topic": topic if trigger == TRIGGER_ASKS_ABOUT else "",
            "steps": steps,
            "active": active,
        })
    return out


def get_flows(db: Session, tenant_id: uuid.UUID) -> list[dict]:
    raw = get_or_create_tenant_profile(db, tenant_id).ai_flows
    return [f for f in raw if isinstance(f, dict)] if isinstance(raw, list) else []


def save_flows(db: Session, tenant_id: uuid.UUID, flows: list[Any], shortcut_ids: set[str]) -> list[dict]:
    profile = get_or_create_tenant_profile(db, tenant_id)
    profile.ai_flows = normalize_flows(flows, shortcut_ids)
    db.flush()
    return profile.ai_flows


def _sent_key(conversation_id, flow_id: str) -> str:
    return f"ai:flow_sent:{conversation_id}:{flow_id}"


def flow_sent(conversation_id, flow_id: str) -> bool:
    try:
        return bool(get_redis().exists(_sent_key(conversation_id, flow_id)))
    except Exception:
        return False


def mark_flow_sent(conversation_id, flow_id: str) -> None:
    try:
        get_redis().set(_sent_key(conversation_id, flow_id), str(int(time.time())), ex=FLOW_SENT_TTL_SECONDS)
    except Exception:
        log.warning("No se pudo marcar flujo enviado conv=%s", conversation_id, exc_info=True)


def describe_steps(flow: dict, shortcuts: list[dict]) -> str:
    by_id = {str(s.get("id")): s for s in shortcuts}
    parts = []
    for index, step in enumerate(flow.get("steps") or [], start=1):
        if step["type"] == "text":
            text = " ".join(step["text"].split())
            parts.append(f'{index}. mensaje: "{text[:160]}{"…" if len(text) > 160 else ""}"')
        else:
            shortcut = by_id.get(step["shortcut_id"]) or {}
            kind = {"image": "foto", "document": "PDF"}.get(shortcut.get("type"), "texto")
            parts.append(f"{index}. atajo «{shortcut.get('label') or '?'}» ({kind})")
    return " ".join(parts)


@dataclass(frozen=True)
class FlowPlan:
    prompt: str = ""
    valid_ids: frozenset = frozenset()
    # El saludo: se envía solo, sin que la IA lo elija.
    auto_id: Optional[str] = None


def plan_flows(
    flows: list[dict], shortcuts: list[dict], *, is_first_contact: bool, sent: set[str]
) -> FlowPlan:
    active = [f for f in flows if f.get("active", True) and f["id"] not in sent]
    greeting = next((f for f in active if f["trigger"] == TRIGGER_FIRST_CONTACT), None) if is_first_contact else None
    topics = [f for f in active if f["trigger"] == TRIGGER_ASKS_ABOUT]
    blocks = []
    if greeting:
        blocks.append(
            "\n\nEste es el primer mensaje del cliente. Antes de tu respuesta se le envía automáticamente el saludo "
            f"del negocio: {describe_steps(greeting, shortcuts)}\n"
            "- No saludes otra vez ni repitas lo que ya dice ese saludo.\n"
            '- Si el cliente solo saludó, o el saludo ya responde lo que preguntó, deja "message" vacío ("").\n'
            "- Si preguntó algo concreto que el saludo no responde, respóndelo corto."
        )
    if topics:
        lines = "\n".join(
            f'- flow_id="{f["id"]}" | cuando el cliente pregunta por: {f["topic"]} | envía: {describe_steps(f, shortcuts)}'
            for f in topics
        )
        blocks.append(
            "\n\nFlujos del negocio (mensajes ya preparados que se envían exactos y en orden):\n"
            f"{lines}\n"
            '- Si lo que el cliente escribe ahora encaja con un flujo (aunque lo diga con otras palabras), agrega al '
            'JSON la clave "flow_id" con su id. Se envía antes de tu "message".\n'
            '- Con flow_id, tu "message" solo complementa en una frase corta, o va vacío (""). No repitas lo que '
            "dice el flujo, no prometas enviar lo que el flujo ya envía y no pongas shortcut_id de un atajo que ya "
            "va en el flujo.\n"
            '- Si ningún flujo encaja, no pongas "flow_id".'
        )
    return FlowPlan(
        prompt="".join(blocks),
        valid_ids=frozenset(f["id"] for f in topics),
        auto_id=greeting["id"] if greeting else None,
    )


def plan_for_conversation(db: Session, tenant_id: uuid.UUID, conversation_id, shortcuts: list[dict], *, is_first_contact: bool) -> FlowPlan:
    flows = get_flows(db, tenant_id)
    if not flows:
        return FlowPlan()
    sent = {f["id"] for f in flows if flow_sent(conversation_id, f["id"])}
    return plan_flows(flows, shortcuts, is_first_contact=is_first_contact, sent=sent)


def send_flow(db: Session, *, tenant, session, conversation, flow_id: str) -> int:
    """Envía los pasos en orden. Se marca antes de enviar: un reintento nunca lo repite."""
    from app.application.ai.ai_shortcut_service import send_shortcut_content
    from app.application.outbound.quick_shortcut_service import get_shortcuts
    from app.application.whatsapp.whatsapp_service import send_text_message
    from app.domain.entities.enums import MessageSource

    flow = next((f for f in get_flows(db, tenant.id) if f["id"] == flow_id), None)
    if flow is None or flow_sent(conversation.id, flow_id):
        return 0
    mark_flow_sent(conversation.id, flow_id)
    shortcuts = {str(s.get("id")): s for s in get_shortcuts(db, tenant.id)}
    sent = 0
    for step in flow["steps"]:
        if step["type"] == "text":
            send_text_message(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                text=step["text"],
                source=MessageSource.BOT.value,
                verbatim=True,
            )
        else:
            shortcut = shortcuts.get(step["shortcut_id"])
            if shortcut is None:
                continue
            send_shortcut_content(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                shortcut=shortcut,
                source=MessageSource.BOT.value,
            )
        sent += 1
    log.info("Flujo enviado conv=%s flow=%s pasos=%s", conversation.id, flow_id, sent)
    return sent

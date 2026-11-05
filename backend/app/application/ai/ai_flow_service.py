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
SENT_FLOW_CHARS = 1200


class FlowError(ValueError):
    """Mensaje listo para mostrarle al dueño."""


def _clean(text: Any, limit: int) -> str:
    return str(text or "").strip()[:limit]


def _file_step(step: dict, asset_prefix: str) -> Optional[dict]:
    path = str(step.get("path") or "").strip()
    if not asset_prefix or not path.startswith(asset_prefix) or ".." in path:
        return None
    if step["type"] == "image":
        return {"type": "image", "path": path}
    name = " ".join(_clean(step.get("file_name"), 120).split()) or "archivo.pdf"
    return {"type": "document", "path": path, "file_name": name}


def normalize_flows(raw: list[Any], shortcut_ids: set[str], asset_prefix: str = "") -> list[dict]:
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
            elif step.get("type") in ("image", "document") and (file_step := _file_step(step, asset_prefix)):
                steps.append(file_step)
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
    profile.ai_flows = normalize_flows(flows, shortcut_ids, asset_prefix=f"assets/{tenant_id}/")
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
        elif step["type"] == "image":
            parts.append(f"{index}. una foto")
        elif step["type"] == "document":
            parts.append(f"{index}. el PDF «{step['file_name']}»")
        else:
            shortcut = by_id.get(step["shortcut_id"]) or {}
            kind = {"image": "foto", "document": "PDF"}.get(shortcut.get("type"), "texto")
            parts.append(f"{index}. atajo «{shortcut.get('label') or '?'}» ({kind})")
    return " ".join(parts)


def step_as_shortcut(step: dict) -> Optional[dict]:
    """Foto o PDF subido directo al flujo, con la forma de un atajo para enviarlo igual."""
    if step["type"] == "image":
        return {"type": "image", "image_path": step["path"], "label": "Foto"}
    if step["type"] == "document":
        return {"type": "document", "file_path": step["path"], "file_name": step["file_name"], "label": step["file_name"]}
    return None


@dataclass(frozen=True)
class FlowPlan:
    prompt: str = ""
    valid_ids: frozenset = frozenset()
    # El saludo: se envía solo, sin que la IA lo elija.
    auto_id: Optional[str] = None
    # (id, tema) de los flujos que aún se pueden enviar: el enrutador elige por intención.
    topics: tuple[tuple[str, str], ...] = ()
    routed_id: Optional[str] = None
    # (id, atajos) de cada flujo: el archivo que ya va en el flujo no se manda otra vez como atajo.
    flow_files: tuple[tuple[str, frozenset], ...] = ()

    def files_of(self, flow_id: Optional[str]) -> frozenset:
        return next((files for fid, files in self.flow_files if fid == flow_id), frozenset())


def _flow_shortcut_ids(flow: dict) -> frozenset:
    return frozenset(str(step.get("shortcut_id")) for step in flow.get("steps") or [] if step.get("shortcut_id"))


def _full_text(flow: dict) -> str:
    texts = [" ".join(step["text"].split()) for step in flow.get("steps") or [] if step["type"] == "text"]
    return " / ".join(texts)[:SENT_FLOW_CHARS]


def plan_flows(
    flows: list[dict], shortcuts: list[dict], *, is_first_contact: bool, sent: set[str]
) -> FlowPlan:
    already = [f for f in flows if f.get("active", True) and f["id"] in sent and f["trigger"] == TRIGGER_ASKS_ABOUT]
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
            '- Decide por la INTENCIÓN del cliente, no por las palabras exactas: «¿cómo se separa?», «¿cómo '
            'contratamos?», «¿qué hago para apartarlo?» y «quiero reservar» piden lo mismo. Si encaja con un flujo, '
            'agrega al JSON la clave "flow_id" con su id. Se envía antes de tu "message".\n'
            "- No confundas etapas: que diga que le interesa o que eligió una opción NO es preguntar cómo "
            "reservar o pagar. Un flujo de reservar, separar o pagar solo va si pregunta cómo hacerlo o dice que "
            "quiere hacerlo ya; antes resuelve sus dudas de la opción.\n"
            '- Si encaja con varios flujos parecidos (ej. reservar paquetes distintos), escoge por lo que han venido '
            "hablando o el paquete que eligió. Si aun así no se sabe cuál, no pongas flow_id: pregúntale en una "
            "frase corta cuál de esas opciones le interesa.\n"
            '- Con flow_id, tu "message" solo complementa en una frase corta, o va vacío (""). No repitas lo que '
            "dice el flujo, no prometas enviar lo que el flujo ya envía y no pongas shortcut_id de un atajo que ya "
            "va en el flujo.\n"
            '- Si ningún flujo encaja, no pongas "flow_id".'
        )
    if already:
        labels = {str(s.get("id")): str(s.get("label") or "").strip() for s in shortcuts}

        def sent_line(flow: dict) -> str:
            files = [labels[i] for i in _flow_shortcut_ids(flow) if labels.get(i)]
            attached = f" (ya incluyó el archivo {', '.join(f'«{n}»' for n in files)})" if files else ""
            return f"- {flow['topic']}{attached}: «{_full_text(flow)}»"

        lines = "\n".join(sent_line(f) for f in already if _full_text(f))
        if lines:
            blocks.append(
                "\n\nFlujos que ya se le enviaron en este chat (no los reenvíes, ni sus archivos como atajo, salvo "
                "que el cliente diga que no le llegó). Si vuelve a preguntar por eso, respóndele con esta misma "
                f"información, sin cambiarla ni agregar pasos:\n{lines}"
            )
    return FlowPlan(
        prompt="".join(blocks),
        valid_ids=frozenset(f["id"] for f in topics),
        auto_id=greeting["id"] if greeting else None,
        topics=tuple((f["id"], f["topic"]) for f in topics),
        flow_files=tuple((f["id"], _flow_shortcut_ids(f)) for f in active),
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
            shortcut = step_as_shortcut(step) or shortcuts.get(step.get("shortcut_id"))
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

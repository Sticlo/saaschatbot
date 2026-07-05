from __future__ import annotations

import logging
from typing import Optional

from app.application.ai.ai_qualify_service import build_qualify_system_prompt
from app.application.ai.ai_shortcut_service import (
    AiGeneratedReply,
    append_shortcuts_instructions,
    parse_ai_reply,
    shortcut_ids,
)
from app.config import settings
from app.domain.entities import Message, Tenant, TenantProfile
from app.domain.entities.enums import MessageDirection
from app.infrastructure.ai.deepseek_client import DeepSeekError, chat_completion

log = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """Eres el asistente de ventas por WhatsApp de {business_name}.
Responde en español colombiano, cercano y profesional (tú).
Mensajes cortos (1-3 párrafos breves), sin listas largas.
Objetivo: calificar interés, responder dudas y proponer un siguiente paso concreto.
No inventes precios ni promesas que no estén en el contexto del negocio.
Si no sabes algo, dilo con honestidad y ofrece que un humano del equipo le confirme.
"""


def build_system_prompt(
    tenant: Tenant,
    profile: Optional[TenantProfile],
    *,
    shortcuts: list[dict] | None = None,
) -> str:
    if profile and profile.ai_system_prompt and profile.ai_system_prompt.strip():
        base = profile.ai_system_prompt.strip()
    else:
        base = DEFAULT_SYSTEM_PROMPT.format(business_name=tenant.business_name)
        extras: list[str] = []
        if profile and profile.onboarding_answers:
            answers = profile.onboarding_answers
            if isinstance(answers, dict):
                for key, value in answers.items():
                    if value:
                        extras.append(f"- {key}: {value}")

        if extras:
            base += "\n\nContexto del negocio:\n" + "\n".join(extras)

    return append_shortcuts_instructions(base, shortcuts or [])


def format_history(messages: list[Message], *, limit: Optional[int] = None) -> list[dict[str, str]]:
    cap = limit if limit is not None else settings.ai_history_messages
    rows = messages[-cap:]
    formatted: list[dict[str, str]] = []
    for msg in rows:
        role = "user" if msg.direction == MessageDirection.IN.value else "assistant"
        body = (msg.body or "").strip()
        if not body:
            continue
        formatted.append({"role": role, "content": body})
    return formatted


def _complete_reply(
    *,
    system: str,
    history: list[Message],
    shortcuts: list[dict],
) -> AiGeneratedReply:
    messages = [{"role": "system", "content": system}, *format_history(history)]
    ids = shortcut_ids(shortcuts)
    temperature = 0.45 if ids else 0.65
    raw = chat_completion(
        messages,
        temperature=temperature,
        max_tokens=settings.ai_reply_max_tokens,
    )
    return parse_ai_reply(raw, valid_ids=ids)


def generate_qualify_reply(
    *,
    tenant: Tenant,
    profile: Optional[TenantProfile],
    contact_name: str,
    history: list[Message],
    is_first_contact: bool = False,
    shortcuts: list[dict] | None = None,
) -> AiGeneratedReply:
    shortcut_rows = shortcuts or []
    system = build_qualify_system_prompt(
        tenant,
        profile,
        is_first_contact=is_first_contact,
        shortcuts=shortcut_rows,
    )
    if contact_name and not contact_name.startswith("+"):
        system += f"\n\nNombre del contacto: {contact_name}"

    return _complete_reply(system=system, history=history, shortcuts=shortcut_rows)


def generate_reply(
    *,
    tenant: Tenant,
    profile: Optional[TenantProfile],
    contact_name: str,
    history: list[Message],
    shortcuts: list[dict] | None = None,
) -> AiGeneratedReply:
    shortcut_rows = shortcuts or []
    system = build_system_prompt(tenant, profile, shortcuts=shortcut_rows)
    if contact_name and not contact_name.startswith("+"):
        system += f"\n\nNombre del contacto: {contact_name}"

    try:
        return _complete_reply(system=system, history=history, shortcuts=shortcut_rows)
    except DeepSeekError:
        log.exception("Error generando respuesta DeepSeek tenant=%s", tenant.id)
        raise

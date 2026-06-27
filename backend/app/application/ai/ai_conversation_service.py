from __future__ import annotations

import logging
from typing import Optional

from app.application.ai.ai_qualify_service import build_qualify_system_prompt
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


def build_system_prompt(tenant: Tenant, profile: Optional[TenantProfile]) -> str:
    if profile and profile.ai_system_prompt and profile.ai_system_prompt.strip():
        return profile.ai_system_prompt.strip()

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
    return base


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


def generate_qualify_reply(
    *,
    tenant: Tenant,
    profile: Optional[TenantProfile],
    contact_name: str,
    history: list[Message],
    is_first_contact: bool = False,
) -> str:
    system = build_qualify_system_prompt(
        tenant, profile, is_first_contact=is_first_contact
    )
    if contact_name and not contact_name.startswith("+"):
        system += f"\n\nNombre del contacto: {contact_name}"

    messages = [{"role": "system", "content": system}, *format_history(history)]
    return chat_completion(
        messages,
        temperature=0.65,
        max_tokens=settings.ai_reply_max_tokens,
    )


def generate_reply(
    *,
    tenant: Tenant,
    profile: Optional[TenantProfile],
    contact_name: str,
    history: list[Message],
) -> str:
    system = build_system_prompt(tenant, profile)
    if contact_name and not contact_name.startswith("+"):
        system += f"\n\nNombre del contacto: {contact_name}"

    messages = [{"role": "system", "content": system}, *format_history(history)]

    try:
        return chat_completion(
            messages,
            temperature=0.65,
            max_tokens=settings.ai_reply_max_tokens,
        )
    except DeepSeekError:
        log.exception("Error generando respuesta DeepSeek tenant=%s", tenant.id)
        raise

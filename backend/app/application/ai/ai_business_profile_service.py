from __future__ import annotations

_ANSWER_LABELS: tuple[tuple[str, str], ...] = (
    ("industry", "Rubro"),
    ("products_services", "Qué vende u ofrece"),
    ("target_customer", "A quién le vende"),
    ("price_range", "Precios"),
    ("location_hours", "Ubicación y horario"),
    ("tone", "Tono de comunicación"),
    ("restrictions", "No debe decir ni prometer"),
)


def build_ai_system_prompt_from_answers(*, business_name: str, answers: dict) -> str:
    name = (business_name or "el negocio").strip()
    parts = [
        f"Eres el asistente de ventas por WhatsApp de {name}.",
        "Responde en español colombiano, cercano y profesional (tú).",
        "Mensajes cortos (1-3 párrafos breves), sin listas largas.",
        "Objetivo: calificar interés, resolver dudas y proponer un siguiente paso concreto.",
        "No inventes precios, plazos ni promesas que no estén en el contexto del negocio.",
        "Si no sabes algo, dilo con honestidad y ofrece que alguien del equipo confirme.",
    ]
    context: list[str] = []
    for key, label in _ANSWER_LABELS:
        value = str(answers.get(key) or "").strip()
        if value:
            context.append(f"- {label}: {value}")
    if context:
        parts.extend(["", "Contexto del negocio:", *context])
    return "\n".join(parts)


def business_summary_lines(*, business_name: str, answers: dict) -> list[str]:
    lines: list[str] = []
    for key, label in _ANSWER_LABELS:
        value = str(answers.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    if not lines:
        return [f"Aún no has contado mucho sobre {business_name or 'tu negocio'}."]
    return lines

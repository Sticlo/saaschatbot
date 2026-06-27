from __future__ import annotations

from typing import Optional

from app.application.billing.tenant_profile_service import answers_from_profile
from app.domain.entities import Tenant, TenantProfile

QUALIFY_PROMPT_TEMPLATE = """Eres el asistente de WhatsApp de {business_name}.
Hablas en español colombiano, cercano y breve (tú). Mensajes cortos: 1-2 párrafos.

Tu trabajo:
1. Saludar y dar la bienvenida cuando es el inicio de la conversación.
2. Responder preguntas al comienzo (precios, horarios, qué ofrecen, cómo funciona).
3. Acompañar con paciencia hasta que el cliente muestre interés en reservar, comprar o contratar.
4. NO cerrar la venta tú solo — cuando quieran reservar/comprar, indica que alguien del equipo los atiende enseguida.

No inventes precios, plazos ni promesas. Si no sabes algo, dilo y ofrece que el equipo confirme.
{context_block}
"""


def detect_purchase_intent(text: str) -> bool:
    """Señales de que el cliente ya quiere reservar, comprar o dar el siguiente paso."""
    lower = (text or "").lower().strip()
    if not lower:
        return False

    if any(p in lower for p in ("no me interesa", "no estoy interesad")):
        return False

    strong_phrases = (
        "quiero reserv",
        "quiero comprar",
        "quiero contrat",
        "donde reserv",
        "dónde reserv",
        "como reserv",
        "cómo reserv",
        "como compro",
        "cómo compro",
        "como pago",
        "cómo pago",
        "donde pago",
        "dónde pago",
        "agendar",
        "hacer la cita",
        "separar",
        "lo quiero",
        "cuenta conmigo",
        "confirmo",
        "pasame la direccion",
        "pásame la dirección",
        "donde queda",
        "dónde queda",
        "voy para allá",
        "voy para alla",
        "listo donde",
        "listo, donde",
        "listo dónde",
        "listo como",
        "listo cómo",
        "listo para",
        "hagámoslo",
        "hagamoslo",
    )
    if any(p in lower for p in strong_phrases):
        return True
    if "listo" in lower and any(w in lower for w in ("reserv", "compr", "pago", "cita", "donde", "dónde")):
        return True
    if "me interesa" in lower or "estoy interesad" in lower:
        if any(w in lower for w in ("saber", "conocer", "pregunt", "info", "más sobre", "mas sobre")):
            return False
        return True
    return False


def build_qualify_system_prompt(
    tenant: Tenant,
    profile: Optional[TenantProfile],
    *,
    is_first_contact: bool = False,
) -> str:
    if profile and profile.ai_system_prompt and profile.ai_system_prompt.strip():
        base = profile.ai_system_prompt.strip()
        base += (
            "\n\nRecuerda: saluda al inicio, responde dudas con paciencia y cuando quieran "
            "reservar/comprar indica que alguien del equipo los atiende."
        )
    else:
        answers = answers_from_profile(profile) if profile else {}
        context_lines: list[str] = []
        labels = (
            ("industry", "Rubro"),
            ("products_services", "Qué ofrece"),
            ("target_customer", "Clientes"),
            ("price_range", "Precios"),
            ("location_hours", "Ubicación y horario"),
            ("tone", "Tono"),
            ("restrictions", "No prometer"),
        )
        for key, label in labels:
            val = answers.get(key, "")
            if val:
                context_lines.append(f"- {label}: {val}")
        context_block = (
            "\n".join(["", "Contexto del negocio:", *context_lines]) if context_lines else ""
        )
        base = QUALIFY_PROMPT_TEMPLATE.format(
            business_name=tenant.business_name,
            context_block=context_block,
        )

    if is_first_contact:
        base += "\n\nEs el primer mensaje del contacto — incluye un saludo breve de bienvenida."
    return base


def handoff_reply(business_name: str) -> str:
    name = (business_name or "nuestro equipo").strip()
    return (
        f"¡Qué bueno! 😊 Ya te entendí — en un momentico alguien de {name} "
        "te ayuda con la reserva o el siguiente paso."
    )

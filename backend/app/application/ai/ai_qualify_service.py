from __future__ import annotations

import random
import re
from typing import Optional

from app.application.ai.ai_shortcut_service import append_shortcuts_instructions
from app.application.billing.tenant_profile_service import answers_from_profile
from app.domain.entities import Tenant, TenantProfile

QUALIFY_PROMPT_TEMPLATE = """Eres el asistente de WhatsApp de {business_name}.
Hablas en español colombiano, cercano y breve (tú). Mensajes cortos: 1-2 párrafos.

Tu trabajo:
1. Saludar y dar la bienvenida cuando es el inicio de la conversación.
2. Responder preguntas (precios, horarios, qué ofrecen, cómo funciona) y enviar el catálogo si lo piden.
3. Acompañar con paciencia hasta que el cliente decida comprar, reservar o contratar.
4. NO cierres la venta tú ni pidas datos de pago: cuando ya quiera comprar, dile con naturalidad que ya le confirmas.

No inventes precios, plazos ni promesas. Si no sabes algo, dilo y ofrece confirmarlo.
{context_block}
"""

_NO_INTEREST = ("no me interesa", "no estoy interesad")
_INTEREST_RE = re.compile(r"\b(cat[aá]logo|portafolio|la carta|men[uú])\b")


def detect_closing_intent(text: str) -> bool:
    """El cliente ya decidió: quiere reservar, comprar, pagar o ir."""
    lower = (text or "").lower().strip()
    if not lower or any(p in lower for p in _NO_INTEREST):
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
        # Gemini marca así las fotos de transferencias (Nequi, Daviplata…): el cliente ya pagó.
        "comprobante de pago",
    )
    if any(p in lower for p in strong_phrases):
        return True
    return "listo" in lower and any(
        w in lower for w in ("reserv", "compr", "pago", "cita", "donde", "dónde")
    )


def detect_interest_signal(text: str) -> bool:
    """Interés real sin haber decidido aún: pide el catálogo o dice que le interesa."""
    lower = (text or "").lower().strip()
    if not lower or any(p in lower for p in _NO_INTEREST):
        return False
    if _INTEREST_RE.search(lower):
        return True
    if "me interesa" in lower or "estoy interesad" in lower:
        return not any(
            w in lower for w in ("saber", "conocer", "pregunt", "info", "más sobre", "mas sobre")
        )
    return False


def detect_purchase_intent(text: str) -> bool:
    return detect_closing_intent(text) or detect_interest_signal(text)


def build_qualify_system_prompt(
    tenant: Tenant,
    profile: Optional[TenantProfile],
    *,
    is_first_contact: bool = False,
    shortcuts: list[dict] | None = None,
) -> str:
    if profile and profile.ai_system_prompt and profile.ai_system_prompt.strip():
        base = profile.ai_system_prompt.strip()
        base += (
            "\n\nRecuerda: saluda al inicio, responde dudas con paciencia y NO cierres la venta. "
            "Cuando ya quieran comprar, dile con naturalidad que ya le confirmas."
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
    return append_shortcuts_instructions(base, shortcuts or [])


# Sin «momentico»: quien confirma es una persona y puede tardar; el cliente no debe quedar esperando.
CLOSING_HOLD_REPLIES = (
    "¡De una! 😊 Ya quedó anotado; por aquí mismo te confirmo los detalles.",
    "¡Listo! Te dejo anotado 🙌 Por este chat te confirmo lo que falte.",
    "¡Claro que sí! Quedó anotado 😊 Por aquí te confirmo los detalles.",
)


def handoff_reply(business_name: str = "") -> str:
    """Lo que diría alguien del negocio antes de atender en persona: sin mencionar asesores."""
    return random.choice(CLOSING_HOLD_REPLIES)

"""Lo mínimo para que la IA venda sin decir «te confirmo»: qué vende, precios, cómo le pagan,
envíos y dónde/cuándo atiende. Sin esto la IA deja preguntas a medias y gasta respuestas."""
from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.application.billing.tenant_profile_service import answers_from_profile
from app.application.outbound.quick_shortcut_service import get_shortcuts
from app.application.products.product_service import list_products
from app.domain.entities import Product, TenantProfile

# «Nequi» solo no sirve: el cliente necesita el número o saber que paga al recibir.
_PAYMENT_DETAIL_RE = re.compile(
    r"(?:\d[\s.-]?){7,}|efectivo|contra\s?entrega|dat[aá]fono|tarjeta|link|llave|bre-?b|@|\bqr\b"
    r"|en el (?:local|negocio|sitio)|al llegar|presencial",
    re.IGNORECASE,
)
# «Corte $25.000», «barba 15.000», «25 mil».
_WRITTEN_PRICE_RE = re.compile(r"\$\s?\d|\d{1,3}(?:[.,]\d{3})+|\d+\s?mil\b", re.IGNORECASE)

_ITEMS = (
    ("products", "biz-products", "Qué vendes", "Escribe qué vendes u ofreces, o carga tus productos en Inventario."),
    (
        "prices",
        "inventory",
        "Precios en Inventario",
        "Carga en Inventario lo que vendes con su precio exacto: es lo que usa la IA para cotizar. "
        "Un rango («desde $45.000») no le sirve para dar un total.",
    ),
    ("payments", "biz-payments", "Cómo te pagan", "Escribe cómo te pagan, tal cual se lo darías a un cliente."),
    (
        "delivery",
        "biz-delivery",
        "Envíos o entregas",
        "A dónde envías, cuánto cuesta y cuánto tarda. Si no envías, escribe «Solo se recoge en el local».",
    ),
    (
        "location",
        "biz-hours",
        "Dónde estás y tu horario",
        "Ciudad, barrio y horario: «Medellín, Laureles — lun a sáb 9am-7pm».",
    ),
)
_PAYMENT_NEEDS_DETAIL = (
    "Falta el número o la cuenta: escribe «Nequi 300 123 4567 a nombre de Laura», no solo «Nequi»."
)


_IMPORT_CATALOG_HINT = (
    "Ya subiste tu catálogo: impórtalo a Inventario con un clic y revisa que los precios estén bien."
)
_SERVICE_PRICES_HINT = (
    "Escribe en «Qué vendes» el precio de cada servicio: «Corte $25.000, barba $15.000». "
    "Si también vendes productos, cárgalos en Inventario."
)


def _unpriced_hint(names: list[str]) -> str:
    shown = ", ".join(names[:3])
    more = f" y {len(names) - 3} más" if len(names) > 3 else ""
    return f"Sin precio: {shown}{more}. Pónselo en Inventario; sin precio la IA tiene que decir «te confirmo»."


def _only_bookings(products: list[Product], booking: bool) -> bool:
    """Negocio de citas sin productos en Inventario: no envía nada y cobra servicios."""
    return booking and not products


def _missing_keys(answers: dict, products: list[Product], has_catalog: bool, booking: bool) -> dict[str, str]:
    """{clave: pista} de lo que falta."""
    missing: dict[str, str] = {}
    if not answers.get("products_services") and not products:
        missing["products"] = ""
    unpriced = [p.name for p in products if not p.price_cop]
    if _only_bookings(products, booking):
        written = f"{answers.get('products_services', '')} {answers.get('price_range', '')}"
        if not _WRITTEN_PRICE_RE.search(written):
            missing["prices"] = _IMPORT_CATALOG_HINT if has_catalog else _SERVICE_PRICES_HINT
    elif not products:
        missing["prices"] = _IMPORT_CATALOG_HINT if has_catalog else ""
    elif unpriced:
        missing["prices"] = _unpriced_hint(unpriced)
    payments = answers.get("payment_methods", "")
    if not payments:
        missing["payments"] = ""
    elif not _PAYMENT_DETAIL_RE.search(payments):
        missing["payments"] = _PAYMENT_NEEDS_DETAIL
    if not answers.get("delivery_info") and not _only_bookings(products, booking):
        missing["delivery"] = ""
    if not answers.get("location_hours"):
        missing["location"] = ""
    return missing


def readiness_from(
    answers: dict, products: list[Product], *, has_catalog: bool = False, booking: bool = False
) -> dict:
    missing = _missing_keys(answers, products, has_catalog, booking)
    only_bookings = _only_bookings(products, booking)
    items = []
    for key, field, label, hint in _ITEMS:
        if key == "delivery" and only_bookings:
            continue
        if key == "prices" and only_bookings:
            field, label = "biz-products", "Precios de tus servicios"
        item = {"key": key, "field": field, "label": label, "done": key not in missing, "hint": missing.get(key) or hint}
        if key == "prices" and missing.get(key) == _IMPORT_CATALOG_HINT:
            item["action"] = "import-catalog"
        items.append(item)
    return {
        "ready": not missing,
        "done": len(items) - len(missing),
        "total": len(items),
        "items": items,
    }


def has_catalog_text(db: Session, tenant_id: uuid.UUID) -> bool:
    """Lo que la IA leyó de los catálogos subidos (fotos o PDF) se puede importar a Inventario."""
    return any(s.get("content") for s in get_shortcuts(db, tenant_id))


def tenant_readiness(db: Session, tenant_id: uuid.UUID) -> dict:
    profile = db.query(TenantProfile).filter(TenantProfile.tenant_id == tenant_id).first()
    answers = answers_from_profile(profile) if profile is not None else {}
    return readiness_from(
        answers,
        list_products(db, tenant_id),
        has_catalog=has_catalog_text(db, tenant_id),
        booking=bool(profile is not None and profile.ai_booking_enabled),
    )


# «Ropa unisex», «Buzos hombre», «Tortas»: una categoría, no algo que el cliente pueda pedir.
_GENERIC_NAME_RE = re.compile(
    r"^(?:ropa|productos?|varios|surtidos?|accesorios|catalogo|mercancia|articulos|referencias|prendas|\w{3,}s)\b"
)
_SHIPS_RE = re.compile(r"env[ií]o|domicilio|interrapid|servientrega|coordinadora|transportadora|encomienda", re.IGNORECASE)
GENERIC_CATALOG_MAX = 3


def _tip(key: str, label: str, hint: str, action: str) -> dict:
    return {"key": key, "label": label, "hint": hint, "action": action}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _plain_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower().strip()


def sales_tips(
    *,
    answers: dict,
    products: list[Product],
    recipients: list[dict],
    own_number: bool,
    booking: bool,
    tried: bool,
) -> list[dict]:
    """Lo que no impide prender la IA pero le hace perder ventas al negocio sin que se entere."""
    tips: list[dict] = []
    if own_number:
        tips.append(
            _tip(
                "alerts-own",
                "Tus alertas van al número del negocio",
                "Te llegan como un mensaje a ti mismo, sin sonar. Pon tu WhatsApp personal o el de quien despacha.",
                "alerts",
            )
        )
    elif not recipients:
        tips.append(
            _tip(
                "alerts",
                "Nadie recibe el aviso cuando alguien quiere comprar",
                "Pon tu WhatsApp personal en Alertas: te escribimos apenas la IA cierre un pedido o llegue un comprobante.",
                "alerts",
            )
        )
    no_photo = [p for p in products if not p.photos]
    if no_photo:
        tips.append(
            _tip(
                "photos",
                _plural(len(no_photo), "producto sin foto", "productos sin foto"),
                "Casi todos piden ver antes de comprar. Sube al menos una foto por producto: la IA la manda sola.",
                "inventory",
            )
        )
    generic = [p for p in products if _GENERIC_NAME_RE.match(_plain_name(p.name))]
    if products and len(products) <= GENERIC_CATALOG_MAX and generic:
        tips.append(
            _tip(
                "catalog",
                "Tu catálogo es muy general",
                f"«{generic[0].name}» es una categoría. Si vendes varias referencias, sube cada una aparte con su foto, "
                "precio y detalles (tallas, colores, medidas): así la IA recomienda algo concreto y cierra.",
                "inventory",
            )
        )
    else:
        no_details = [p for p in products if not (p.details or "").strip()]
        if no_details:
            tips.append(
                _tip(
                    "details",
                    _plural(len(no_details), "producto sin detalles", "productos sin detalles"),
                    "Escribe tallas, colores, medidas o sabores en Detalles: es lo primero que preguntan.",
                    "inventory",
                )
            )
    if booking and products and _SHIPS_RE.search(answers.get("delivery_info") or ""):
        tips.append(
            _tip(
                "booking",
                "Tienes las citas activadas",
                "La IA les ofrece horarios a tus clientes. Si no atiendes con cita (por ejemplo, una tienda que envía), apágalas.",
                "booking-off",
            )
        )
    if not tried:
        tips.append(
            _tip(
                "try",
                "Prueba tu IA antes de que te escriban",
                "Escríbele como si fueras un cliente: pregunta precios, pide fotos y compra. Verás lo mismo que ellos.",
                "try-ai",
            )
        )
    return tips


def tenant_sales_tips(db: Session, tenant_id: uuid.UUID, profile: Optional[TenantProfile]) -> list[dict]:
    from app.application.ai.ai_trial_service import has_tried
    from app.application.conversations.interest_alert_service import alert_recipients, own_number_recipients
    from app.domain.entities import WhatsAppSession

    session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()
    recipients = alert_recipients(profile)
    return sales_tips(
        answers=answers_from_profile(profile) if profile is not None else {},
        products=list_products(db, tenant_id),
        recipients=recipients,
        own_number=bool(own_number_recipients(recipients, session)),
        booking=bool(profile is not None and profile.ai_booking_enabled),
        tried=has_tried(tenant_id),
    )


def not_ready_message(readiness: dict) -> str:
    missing = [i["label"].lower() for i in readiness["items"] if not i["done"]]
    return (
        f"Antes de prender la IA completa en Personalizar IA: {', '.join(missing)}. "
        "Así no le dice «te confirmo» a tus clientes y cierra la venta sola."
    )

"""Lo mínimo para que la IA venda sin decir «te confirmo»: qué vende, precios, cómo le pagan,
envíos y dónde/cuándo atiende. Sin esto la IA deja preguntas a medias y gasta respuestas."""
from __future__ import annotations

import re
import uuid

from sqlalchemy.orm import Session

from app.application.billing.tenant_profile_service import answers_from_profile
from app.application.outbound.quick_shortcut_service import get_shortcuts
from app.application.products.product_service import list_products
from app.domain.entities import Product, TenantProfile

# «Nequi» solo no sirve: el cliente necesita el número o saber que paga al recibir.
_PAYMENT_DETAIL_RE = re.compile(
    r"(?:\d[\s.-]?){7,}|efectivo|contra\s?entrega|dat[aá]fono|tarjeta|link|llave|bre-?b|@|\bqr\b",
    re.IGNORECASE,
)

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


def _unpriced_hint(names: list[str]) -> str:
    shown = ", ".join(names[:3])
    more = f" y {len(names) - 3} más" if len(names) > 3 else ""
    return f"Sin precio: {shown}{more}. Pónselo en Inventario; sin precio la IA tiene que decir «te confirmo»."


def _missing_keys(answers: dict, products: list[Product], has_catalog: bool) -> dict[str, str]:
    """{clave: pista} de lo que falta."""
    missing: dict[str, str] = {}
    if not answers.get("products_services") and not products:
        missing["products"] = ""
    unpriced = [p.name for p in products if not p.price_cop]
    if not products:
        missing["prices"] = _IMPORT_CATALOG_HINT if has_catalog else ""
    elif unpriced:
        missing["prices"] = _unpriced_hint(unpriced)
    payments = answers.get("payment_methods", "")
    if not payments:
        missing["payments"] = ""
    elif not _PAYMENT_DETAIL_RE.search(payments):
        missing["payments"] = _PAYMENT_NEEDS_DETAIL
    if not answers.get("delivery_info"):
        missing["delivery"] = ""
    if not answers.get("location_hours"):
        missing["location"] = ""
    return missing


def readiness_from(answers: dict, products: list[Product], *, has_catalog: bool = False) -> dict:
    missing = _missing_keys(answers, products, has_catalog)
    items = []
    for key, field, label, hint in _ITEMS:
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
    return readiness_from(answers, list_products(db, tenant_id), has_catalog=has_catalog_text(db, tenant_id))


def not_ready_message(readiness: dict) -> str:
    missing = [i["label"].lower() for i in readiness["items"] if not i["done"]]
    return (
        f"Antes de prender la IA completa en Personalizar IA: {', '.join(missing)}. "
        "Así no le dice «te confirmo» a tus clientes y cierra la venta sola."
    )

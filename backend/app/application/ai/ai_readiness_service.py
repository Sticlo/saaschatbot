"""Lo mínimo para que la IA venda sin decir «te confirmo»: qué vende, precios, cómo le pagan,
envíos y dónde/cuándo atiende. Sin esto la IA deja preguntas a medias y gasta respuestas."""
from __future__ import annotations

import re
import uuid

from sqlalchemy.orm import Session

from app.application.billing.tenant_profile_service import answers_from_profile
from app.application.products.product_service import list_products
from app.domain.entities import Product, TenantProfile

_DIGIT_RE = re.compile(r"\d")
_PRICE_IN_TEXT_RE = re.compile(
    r"\$\s?\d|\d+(?:[.,]\d+)?\s?(?:mil|k|lucas)\b|\d{1,3}(?:[.,]\d{3})+|\d{4,}", re.IGNORECASE
)
# «Nequi» solo no sirve: el cliente necesita el número o saber que paga al recibir.
_PAYMENT_DETAIL_RE = re.compile(
    r"(?:\d[\s.-]?){7,}|efectivo|contra\s?entrega|dat[aá]fono|tarjeta|link|llave|bre-?b|@|\bqr\b",
    re.IGNORECASE,
)

_ITEMS = (
    ("products", "biz-products", "Qué vendes", "Escribe qué vendes u ofreces, o carga tus productos en Inventario."),
    (
        "prices",
        "biz-prices",
        "Precios",
        "Pon precios con números (ej. «Camisas desde $45.000») o cárgalos en Inventario. "
        "Sin precios la IA no puede cerrar la venta.",
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


def _missing_keys(answers: dict, products: list[Product]) -> dict[str, str]:
    """{clave: pista} de lo que falta."""
    missing: dict[str, str] = {}
    products_text = answers.get("products_services", "")
    if not products_text and not products:
        missing["products"] = ""
    has_price = (
        bool(_DIGIT_RE.search(answers.get("price_range", "")))
        or bool(_PRICE_IN_TEXT_RE.search(products_text))
        or any(p.price_cop for p in products)
    )
    if not has_price:
        missing["prices"] = ""
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


def readiness_from(answers: dict, products: list[Product]) -> dict:
    missing = _missing_keys(answers, products)
    items = [
        {
            "key": key,
            "field": field,
            "label": label,
            "done": key not in missing,
            "hint": missing.get(key) or hint,
        }
        for key, field, label, hint in _ITEMS
    ]
    return {
        "ready": not missing,
        "done": len(items) - len(missing),
        "total": len(items),
        "items": items,
    }


def tenant_readiness(db: Session, tenant_id: uuid.UUID) -> dict:
    profile = db.query(TenantProfile).filter(TenantProfile.tenant_id == tenant_id).first()
    answers = answers_from_profile(profile) if profile is not None else {}
    return readiness_from(answers, list_products(db, tenant_id))


def not_ready_message(readiness: dict) -> str:
    missing = [i["label"].lower() for i in readiness["items"] if not i["done"]]
    return (
        f"Antes de prender la IA completa en Personalizar IA: {', '.join(missing)}. "
        "Así no le dice «te confirmo» a tus clientes y cierra la venta sola."
    )

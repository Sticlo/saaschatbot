"""Señales de que un negocio ofrece algo ilegal: el dueño de la plataforma las revisa y decide; nada se suspende solo."""
from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Any, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.domain.entities import Product, Tenant, TenantProfile

# Fragmentos válidos tanto en Python como en Postgres (sin \b ni lookarounds); se comparan en minúscula y sin tildes.
# Se dejan fuera palabras con usos legales frecuentes (revolver, silenciador, porro, cristal).
RISK_TERMS: tuple[str, ...] = (
    # Drogas
    "cocaina", "perico", "basuco", "bazuco", "marihuana", "cripy", "cripa", "cogollos?", "baretos?",
    "tusi", "tussi", "tusibi", "tucibi", r"2\s*-?\s*cb", "extasis", "mdma", "lsd", "ketamina", "heroina",
    "fentanilo", "metanfetaminas?", "poppers?", r"hongos\s+alucinogenos", r"dosis\s+personal",
    "escopolamina", "burundanga",
    # Armas
    r"armas?\s+de\s+fuego", r"armas?\s+traumaticas?", "municion(es)?", r"pistolas?\s+(9\s*mm|calibre)",
    # Medicamentos controlados o abortivos
    "cytotec", "misoprostol", "rivotril", "clonazepam", "tramadol", r"sin\s+(formula|receta)\s+medica",
    # Falsificación y fraude
    r"billetes?\s+fals[oa]s?",
    r"(documentos?|cedulas?|pases?|licencias?|diplomas?|titulos?|placas?)\s+fals[oa]s?",
    r"clonacion\s+de\s+tarjetas?", r"cuentas?\s+hackeadas?",
)
# Frases legales que contienen un término de riesgo (comida colombiana, sobre todo).
SAFE_PHRASES = re.compile(r"huevos?\s+pericos?|cafe\s+perico|perico\s+(de\s+)?huevos?|cogollos?\s+de\s+\w+")

_ANY = "(" + "|".join(RISK_TERMS) + ")"
_MATCH = re.compile(r"\b" + _ANY + r"\b")
_ACCENTS_FROM = "áéíóúüñÁÉÍÓÚÜÑ"
_ACCENTS_TO = "aeiouunAEIOUUN"
MAX_FINDINGS = 10

PROFILE_FIELDS = (
    ("industry", "Tipo de negocio"),
    ("products_services", "Qué vende"),
    ("ai_system_prompt", "Instrucciones de la IA"),
)


def _normalize(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in plain if not unicodedata.combining(c))


def risky_terms(text: Optional[str]) -> list[str]:
    """Términos de riesgo en el texto, sin repetir y en el orden en que aparecen."""
    if not text:
        return []
    clean = SAFE_PHRASES.sub(" ", _normalize(text))
    found: list[str] = []
    for match in _MATCH.finditer(clean):
        term = re.sub(r"\s+", " ", match.group(0))
        if term not in found:
            found.append(term)
    return found


def _sql_normalized(column):
    # Primero quitar tildes: con locale C, lower() no baja las mayúsculas acentuadas.
    return func.lower(func.translate(func.coalesce(column, ""), _ACCENTS_FROM, _ACCENTS_TO))


def content_findings(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, str]]]:
    """Qué producto o dato del perfil de cada empresa contiene términos de riesgo."""
    out: dict[uuid.UUID, list[dict[str, str]]] = {}
    if not ids:
        return out

    def add(tenant_id: uuid.UUID, where: str, text: Optional[str]) -> None:
        bucket = out.setdefault(tenant_id, [])
        for term in risky_terms(text):
            if len(bucket) < MAX_FINDINGS:
                bucket.append({"term": term, "where": where})

    rows = db.query(Product.tenant_id, Product.name, Product.details).filter(
        Product.tenant_id.in_(ids),
        or_(_sql_normalized(Product.name).op("~")(_ANY), _sql_normalized(Product.details).op("~")(_ANY)),
    )
    for tenant_id, name, details in rows:
        add(tenant_id, f"Producto «{name}»", f"{name}\n{details or ''}")
    for profile in db.query(TenantProfile).filter(TenantProfile.tenant_id.in_(ids)):
        for field, label in PROFILE_FIELDS:
            add(profile.tenant_id, label, getattr(profile, field, None))
    return {tenant_id: found for tenant_id, found in out.items() if found}


def reviewed_terms(tenant: Optional[Tenant]) -> set[str]:
    raw = getattr(tenant, "platform_overrides", None)
    terms = raw.get("content_reviewed") if isinstance(raw, dict) else None
    return set(terms) if isinstance(terms, list) else set()


def pending_terms(tenant: Tenant, findings: list[dict[str, Any]]) -> list[str]:
    """Términos que nadie ha revisado todavía: uno nuevo vuelve a levantar la alerta."""
    seen = reviewed_terms(tenant)
    out: list[str] = []
    for finding in findings:
        if finding["term"] not in seen and finding["term"] not in out:
            out.append(finding["term"])
    return out

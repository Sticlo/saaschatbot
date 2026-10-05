"""Excepciones por empresa: lo que el dueño de la plataforma da o quita por encima del plan."""
from __future__ import annotations

from typing import Any, Optional

from app.domain.entities import Tenant

# Sin ajuste, cada función está disponible y cada límite sale del plan.
FEATURES = {
    "ai_replies": "La IA responde chats",
    "ai_booking": "La IA puede agendar citas",
    "catalog_files": "Subir catálogo PDF o foto a los atajos",
}
LIMITS = {
    "ai_daily_replies": "Respuestas de IA por día (0 = sin límite)",
    "ai_daily_classifications": "Clasificaciones de IA por día (0 = sin límite)",
    "max_team_members": "Usuarios del equipo",
}
MAX_NOTE_CHARS = 2000


def tenant_overrides(tenant: Optional[Tenant]) -> dict[str, Any]:
    raw = getattr(tenant, "platform_overrides", None)
    return raw if isinstance(raw, dict) else {}


def feature_allowed(tenant: Optional[Tenant], key: str) -> bool:
    features = tenant_overrides(tenant).get("features")
    if not isinstance(features, dict) or key not in features:
        return True
    return bool(features[key])


def limit_override(tenant: Optional[Tenant], key: str) -> Optional[int]:
    limits = tenant_overrides(tenant).get("limits")
    if not isinstance(limits, dict) or limits.get(key) is None:
        return None
    try:
        return max(0, int(limits[key]))
    except (TypeError, ValueError):
        return None


def normalize_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Solo claves conocidas; un valor None borra el ajuste y vuelve a lo del plan."""
    out: dict[str, Any] = {}
    features = {
        key: bool(value)
        for key, value in (raw.get("features") or {}).items()
        if key in FEATURES and value is not None
    }
    if features:
        out["features"] = features
    limits: dict[str, int] = {}
    for key, value in (raw.get("limits") or {}).items():
        if key not in LIMITS or value is None or value == "":
            continue
        try:
            limits[key] = max(0, min(int(value), 1_000_000))
        except (TypeError, ValueError):
            raise ValueError(f"Límite inválido: {LIMITS[key]}") from None
    if limits:
        out["limits"] = limits
    note = str(raw.get("note") or "").strip()[:MAX_NOTE_CHARS]
    if note:
        out["note"] = note
    return out

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.application.ai.ai_mode_service import resolve_ai_mode
from app.application.ai.ai_usage_service import build_ai_usage_summary
from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.domain.entities import Tenant
from app.infrastructure.persistence.database import get_db
from app.shared.core.deps import RequireViewer
from app.config import ALLOWED_DEEPSEEK_MODEL, settings
from app.infrastructure.ai.ai_text_provider import is_configured

router = APIRouter(prefix="/ai", tags=["ai"])


@router.get("/status")
def ai_status(current: RequireViewer, db: Session = Depends(get_db)):
    """Estado de la IA para el panel. Las fallas del proveedor no son asunto del dueño (las
    cubren reintentos y el respaldo, y avisan al dev), así que aquí no se exponen."""
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    profile = get_or_create_tenant_profile(db, current.tenant_id) if tenant else None
    usage = build_ai_usage_summary(db, tenant) if tenant else {}

    return {
        "configured": is_configured(),
        "model": ALLOWED_DEEPSEEK_MODEL,
        "ai_mode": resolve_ai_mode(profile),
        "classifier_mode": "llm" if settings.ai_classifier_use_llm else "rules",
        "provider_ok": True,
        "provider_error": None,
        **usage,
    }

from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.application.ai.ai_mode_service import resolve_ai_mode
from app.application.ai.ai_usage_service import build_ai_usage_summary
from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.domain.entities import Tenant
from app.infrastructure.persistence.database import get_db
from app.shared.core.deps import RequireViewer
from app.config import ALLOWED_DEEPSEEK_MODEL, settings
from app.infrastructure.ai.deepseek_client import check_provider_health, is_configured

router = APIRouter(prefix="/ai", tags=["ai"])

_provider_cache: dict[str, object] = {"checked_at": 0.0, "ok": False, "error": None}


@router.get("/status")
def ai_status(current: RequireViewer, db: Session = Depends(get_db)):
    """Estado del proveedor IA (DeepSeek) para el panel."""
    now = time.time()
    if now - float(_provider_cache.get("checked_at") or 0) > 300:
        ok, err = check_provider_health()
        _provider_cache["checked_at"] = now
        _provider_cache["ok"] = ok
        _provider_cache["error"] = err

    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    profile = get_or_create_tenant_profile(db, current.tenant_id) if tenant else None
    usage = build_ai_usage_summary(db, tenant) if tenant else {}

    return {
        "configured": is_configured(),
        "model": ALLOWED_DEEPSEEK_MODEL,
        "ai_mode": resolve_ai_mode(profile),
        "classifier_mode": "llm" if settings.ai_classifier_use_llm else "rules",
        "provider_ok": bool(_provider_cache.get("ok")),
        "provider_error": _provider_cache.get("error"),
        **usage,
    }

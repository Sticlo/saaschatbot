from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter

from app.shared.core.deps import RequireViewer
from app.config import ALLOWED_DEEPSEEK_MODEL
from app.infrastructure.ai.deepseek_client import check_provider_health, is_configured

router = APIRouter(prefix="/ai", tags=["ai"])

_provider_cache: dict[str, object] = {"checked_at": 0.0, "ok": False, "error": None}


@router.get("/status")
def ai_status(current: RequireViewer):
    """Estado del proveedor IA (DeepSeek) para el panel."""
    now = time.time()
    if now - float(_provider_cache.get("checked_at") or 0) > 300:
        ok, err = check_provider_health()
        _provider_cache["checked_at"] = now
        _provider_cache["ok"] = ok
        _provider_cache["error"] = err

    return {
        "configured": is_configured(),
        "model": ALLOWED_DEEPSEEK_MODEL,
        "provider_ok": bool(_provider_cache.get("ok")),
        "provider_error": _provider_cache.get("error"),
    }

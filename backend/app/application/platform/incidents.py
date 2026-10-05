"""Lo que le pasa a cada empresa (fallas, límites, traspasos) queda en su historial para la consola."""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from sqlalchemy import text

from app.domain.entities import AuditLog
from app.infrastructure.cache.redis_client import get_redis
from app.infrastructure.persistence.database import SessionLocal

log = logging.getLogger(__name__)

DEFAULT_THROTTLE_SECONDS = 600

# Acciones system.* que la consola marca como problema (las demás son eventos normales).
PROBLEM_ACTIONS = frozenset(
    {
        "system.ai_send_failed",
        "system.ai_outage_handoff",
        "system.ai_quota_reached",
        "system.whatsapp_disconnected",
    }
)


def record_incident(
    tenant_id: Any,
    action: str,
    message: str,
    *,
    details: Optional[dict[str, Any]] = None,
    throttle_seconds: int = DEFAULT_THROTTLE_SECONDS,
    throttle_scope: str = "",
) -> bool:
    """Sesión propia: el rastro queda aunque el flujo que falló haga rollback.

    Se agrupa por (empresa, acción, alcance) para no llenar el historial con la misma falla."""
    if not tenant_id:
        return False
    if throttle_seconds > 0:
        key = f"platform:incident:{tenant_id}:{action}:{throttle_scope}"
        try:
            if not get_redis().set(key, "1", nx=True, ex=throttle_seconds):
                return False
        except Exception:
            pass
    try:
        with SessionLocal() as db:
            # Si quien llama tiene la empresa a medio crear, mejor perder el rastro que quedarse esperando.
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            db.add(
                AuditLog(
                    tenant_id=tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id)),
                    action=action,
                    details=details or None,
                    message=message[:500],
                )
            )
            db.commit()
        return True
    except Exception:
        log.warning("No se pudo registrar incidente %s tenant=%s", action, tenant_id, exc_info=True)
        return False

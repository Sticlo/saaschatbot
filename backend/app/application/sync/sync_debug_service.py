from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
from app.application.sync.webhook_trace_service import last_webhook_at, list_webhook_trace
from app.application.workers.queue_service import INBOUND_WEBHOOK_QUEUE, webhook_worker_is_alive
from app.application.sync.live_pull_scheduler import live_pull_scheduler_is_alive
from app.infrastructure.cache.redis_client import get_redis
from app.application.chatwoot.chatwoot_service import (
    chatwoot_sync_mode,
    chatwoot_sync_ready,
    chatwoot_token_configured,
)
from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client
from app.infrastructure.evolution.evolution_store import fetch_stored_counts


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def build_sync_debug_report(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
) -> dict[str, Any]:
    started = time.perf_counter()
    errors: list[str] = []
    timing_ms: dict[str, int] = {}

    tenant_id = tenant.id
    instance_name = session.instance_name
    session_status = session.status
    active_connection_id = session.active_connection_id
    phone_number = session.phone_number
    chatwoot_ready = chatwoot_sync_ready(session) if chatwoot_sync_mode() else False
    webhook_url = f"{settings.evolution_webhook_base_url()}/webhooks/evolution/{tenant_id}"

    since = datetime.now(timezone.utc) - timedelta(minutes=15)
    messages_15m = (
        db.query(func.count(Message.id))
        .filter(Message.tenant_id == tenant_id, Message.created_at >= since)
        .scalar()
        or 0
    )
    last_message = (
        db.query(Message.created_at, Message.body)
        .filter(Message.tenant_id == tenant_id)
        .order_by(Message.created_at.desc())
        .first()
    )
    conv_count = (
        db.query(func.count(Conversation.id))
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.whatsapp_connection_id == active_connection_id,
        )
        .scalar()
        or 0
    )
    # El panel pide este reporte cada 20 s por pestaña: soltar la conexión antes de hablar con
    # Evolution y con la propia API evita agotar el pool cuando alguno de los dos se pone lento.
    db.close()

    webhook_reachable: bool | None = None
    webhook_reachable_detail = ""
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{settings.app_public_url.rstrip('/')}/health")
            webhook_reachable = resp.status_code == 200
            webhook_reachable_detail = f"API local health {resp.status_code}"
    except Exception as exc:
        webhook_reachable = False
        webhook_reachable_detail = str(exc)[:200]
    timing_ms["api_health"] = _ms(t0)

    evolution_webhook_config: dict[str, Any] = {}
    t0 = time.perf_counter()
    try:
        found = evolution_client._request(
            "GET",
            f"/webhook/find/{instance_name}",
            timeout=10.0,
        )
        if isinstance(found, dict):
            evolution_webhook_config = {
                "url": found.get("url"),
                "enabled": found.get("enabled"),
                "events": found.get("events") or [],
            }
    except EvolutionAPIError as exc:
        errors.append(f"webhook/find: {exc}")
    timing_ms["evolution_webhook"] = _ms(t0)

    queue_depth = 0
    try:
        queue_depth = int(get_redis().llen(INBOUND_WEBHOOK_QUEUE))
    except Exception as exc:
        errors.append(f"redis queue: {exc}")

    trace = list_webhook_trace(tenant_id, limit=15)
    last_wh = last_webhook_at(tenant_id)

    evolution_db_counts: dict[str, int] = {}
    if settings.evolution_database_url:
        try:
            evolution_db_counts = fetch_stored_counts(
                settings.evolution_database_url, instance_name
            )
        except Exception as exc:
            errors.append(f"evolution_db: {exc}")

    only_test_webhooks = bool(trace) and all(
        str(item.get("message_id") or "").startswith("TEST") for item in trace[:5]
    )

    url_mismatch = False
    configured = evolution_webhook_config.get("url") or ""
    if configured and configured.rstrip("/") != webhook_url.rstrip("/"):
        url_mismatch = True
        errors.append(
            f"Webhook Evolution ({configured}) ≠ esperado ({webhook_url})"
        )

    hints: list[str] = []
    if url_mismatch:
        hints.append(
            "Abre el panel y espera 2 min — el status de WhatsApp re-registra el webhook."
        )
    if not last_wh:
        if url_mismatch:
            hints.append(
                "Ningún webhook recibido en 24h. La URL del webhook en Evolution no coincide "
                "con la esperada — desconecta y vuelve a conectar WhatsApp, o reinicia Evolution."
            )
        elif messages_15m > 0:
            hints.append(
                "Sin webhooks recientes pero sí mensajes en BD — el pull/sync batch funciona; "
                "revisa que la API esté arriba y que Evolution pueda POST a host.docker.internal:8000."
            )
        else:
            hints.append(
                "Ningún webhook recibido. Verifica que la API escuche en :8000, "
                "EVOLUTION_WEBHOOK_SECRET coincida, y reinicia Evolution tras conectar WhatsApp."
            )
    elif messages_15m == 0:
        if only_test_webhooks:
            hints.append(
                "Solo webhooks de prueba (TEST*) — Evolution no está enviando eventos reales. "
                "Reinicia Evolution (./scripts/evolution-mac.sh start) y escribe un mensaje desde el celular."
            )
        elif evolution_db_counts.get("messages", 0) == 0 and session_status == "connected":
            hints.append(
                "Evolution conectado pero sin chats/mensajes en su BD — WhatsApp aún no sincronizó. "
                "Envía o recibe un mensaje desde el celular para arrancar."
            )
        else:
            hints.append(
                "Hay webhooks pero no mensajes nuevos en 15 min — revisa active_connection_id."
            )
    if configured and "host.docker.internal" in configured and not settings.evolution_in_docker:
        hints.append(
            "Webhook Evolution apunta a host.docker.internal pero Evolution corre nativo — "
            "usa APP_PUBLIC_URL=http://localhost:8000, reinicia Evolution y vuelve a conectar."
        )
    if not settings.evolution_database_url:
        hints.append(
            "EVOLUTION_DATABASE_URL vacío — el pull en vivo usará solo la API (más lento)."
        )
    if webhook_reachable is False:
        hints.append(f"La API no responde en {settings.app_public_url}: {webhook_reachable_detail}")

    if settings.chatwoot_enabled and not chatwoot_token_configured():
        hints.append(
            "CHATWOOT_ENABLED sin CHATWOOT_API_TOKEN — usando Evolution como fallback. "
            "Configure CHATWOOT_API_TOKEN para sync vía Chatwoot."
        )
    elif chatwoot_sync_mode() and not chatwoot_ready:
        hints.append(
            "Modo Chatwoot activo pero sin inbox — escanea QR de nuevo o revisa Chatwoot en :3000."
        )
    elif chatwoot_sync_mode():
        hints.append("Modo Chatwoot: chats/mensajes vienen de Chatwoot (no sync Evolution custom).")

    timing_ms["total"] = _ms(started)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timing_ms": timing_ms,
        "errors": errors,
        "hints": hints,
        "session": {
            "instance_name": instance_name,
            "status": session_status,
            "active_connection_id": str(active_connection_id)
            if active_connection_id
            else None,
            "phone_number": phone_number,
        },
        "urls": {
            "app_public_url": settings.app_public_url,
            "webhook_expected": webhook_url,
            "evolution_api": settings.evolution_api_url,
        },
        "webhook": {
            "reachable_from_host": webhook_reachable,
            "reachable_detail": webhook_reachable_detail,
            "evolution_config": evolution_webhook_config,
            "url_mismatch": url_mismatch,
            "last_received_at": last_wh,
            "queue_depth": queue_depth,
            "worker_alive": webhook_worker_is_alive(),
            "live_pull_alive": live_pull_scheduler_is_alive(),
            "recent_trace": trace,
            "only_test_events": only_test_webhooks,
        },
        "evolution_db": evolution_db_counts,
        "messages": {
            "last_15_minutes": messages_15m,
            "last_message_at": last_message.created_at.isoformat() if last_message else None,
            "last_message_preview": ((last_message.body or "")[:80] if last_message else ""),
            "conversations_active": conv_count,
        },
    }

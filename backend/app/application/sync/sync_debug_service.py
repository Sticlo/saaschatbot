from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
from app.application.sync.webhook_trace_service import last_webhook_at, list_webhook_trace
from app.application.workers.queue_service import INBOUND_WEBHOOK_QUEUE, webhook_worker_is_alive
from app.application.sync.live_pull_scheduler import live_pull_scheduler_is_alive
from app.infrastructure.cache.redis_client import get_redis
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

    webhook_url = f"{settings.evolution_webhook_base_url()}/webhooks/evolution/{tenant.id}"
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
            f"/webhook/find/{session.instance_name}",
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

    since = datetime.now(timezone.utc) - timedelta(minutes=15)
    messages_15m = (
        db.query(func.count(Message.id))
        .filter(Message.tenant_id == tenant.id, Message.created_at >= since)
        .scalar()
        or 0
    )
    last_message = (
        db.query(Message)
        .filter(Message.tenant_id == tenant.id)
        .order_by(Message.created_at.desc())
        .first()
    )
    conv_count = (
        db.query(func.count(Conversation.id))
        .filter(
            Conversation.tenant_id == tenant.id,
            Conversation.whatsapp_connection_id == session.active_connection_id,
        )
        .scalar()
        or 0
    )

    trace = list_webhook_trace(tenant.id, limit=15)
    last_wh = last_webhook_at(tenant.id)

    evolution_db_counts: dict[str, int] = {}
    if settings.evolution_database_url:
        try:
            evolution_db_counts = fetch_stored_counts(
                settings.evolution_database_url, session.instance_name
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
        elif evolution_db_counts.get("messages", 0) == 0 and session.status == "connected":
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

    # ── Diagnóstico de chats: fantasmas, confusión de identidad, @lid ──────
    conv_diagnostics = _build_conv_diagnostics(
        db,
        tenant_id=tenant.id,
        connection_id=session.active_connection_id,
    )

    timing_ms["total"] = _ms(started)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timing_ms": timing_ms,
        "errors": errors,
        "hints": hints,
        "session": {
            "instance_name": session.instance_name,
            "status": session.status,
            "active_connection_id": str(session.active_connection_id)
            if session.active_connection_id
            else None,
            "phone_number": session.phone_number,
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
            "last_message_preview": (last_message.body[:80] if last_message else ""),
            "conversations_active": conv_count,
        },
        "conversations": conv_diagnostics,
    }


def _build_conv_diagnostics(
    db: Session,
    *,
    tenant_id,
    connection_id,
) -> dict[str, Any]:
    """Diagnóstico profundo de chats: fantasmas, nombres placeholders, conflictos @lid."""
    from app.shared.core.phone import is_placeholder_contact_name

    if connection_id is None:
        return {"error": "Sin active_connection_id"}

    tid = str(tenant_id)
    cid = str(connection_id)

    # Totales
    total = db.execute(text(
        "SELECT COUNT(*) FROM conversations WHERE tenant_id=:tid AND whatsapp_connection_id=:cid"
    ), {"tid": tid, "cid": cid}).scalar() or 0

    ghost = db.execute(text("""
        SELECT COUNT(*) FROM conversations c
        WHERE c.tenant_id=:tid AND c.whatsapp_connection_id=:cid
        AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id=c.id)
    """), {"tid": tid, "cid": cid}).scalar() or 0

    with_msgs = total - ghost

    # Nombres placeholder (mostrando número en lugar de nombre)
    all_convs = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.whatsapp_connection_id == connection_id,
        )
        .all()
    )
    placeholder_convs = [
        c for c in all_convs
        if is_placeholder_contact_name(c.contact_name, c.contact_phone)
        and c.id in {
            row[0] for row in db.execute(text(
                "SELECT conversation_id FROM messages WHERE conversation_id IN "
                "(SELECT id FROM conversations WHERE tenant_id=:tid AND whatsapp_connection_id=:cid)"
            ), {"tid": tid, "cid": cid}).fetchall()
        }
    ]

    # Conversaciones @lid sin teléfono real
    lid_only = [
        c for c in all_convs
        if (c.contact_jid or "").endswith("@lid")
        and not (c.contact_phone or "").startswith("+")
        and not (c.contact_phone or "").startswith("lid:")
    ]

    # Detectar posibles duplicados: mismo nombre, distinto teléfono
    name_count: dict[str, list] = {}
    for c in all_convs:
        name = (c.contact_name or "").strip()
        if name and not is_placeholder_contact_name(name, c.contact_phone) and len(name) > 3:
            name_count.setdefault(name, []).append(c)
    potential_dups = {
        name: [
            {"id": str(c.id), "phone": c.contact_phone, "jid": c.contact_jid}
            for c in convs
        ]
        for name, convs in name_count.items()
        if len(convs) > 1
    }

    # Top 10 convs con nombre placeholder (para debug)
    placeholder_sample = [
        {
            "name": c.contact_name,
            "phone": c.contact_phone,
            "jid": c.contact_jid,
        }
        for c in sorted(
            placeholder_convs,
            key=lambda x: x.last_message_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )[:10]
    ]

    return {
        "total": total,
        "with_messages": with_msgs,
        "ghost_0_messages": ghost,
        "placeholder_names": len(placeholder_convs),
        "placeholder_sample": placeholder_sample,
        "lid_only_no_phone": len(lid_only),
        "potential_duplicates": potential_dups,
        "duplicate_count": len(potential_dups),
    }

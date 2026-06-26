from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.infrastructure.cache.redis_client import get_redis

log = logging.getLogger(__name__)

_POLL_SECONDS = 5.0
_OVERLAP_SECONDS = 90
_LOOKBACK_SECONDS = 600

_scheduler_thread: Optional[threading.Thread] = None
_scheduler_stop = threading.Event()


def _since_key(tenant_id: uuid.UUID) -> str:
    return f"tenant:{tenant_id}:live_pull_since_ts"


def _read_since_ts(tenant_id: uuid.UUID) -> int:
    now = int(datetime.now(timezone.utc).timestamp())
    try:
        raw = get_redis().get(_since_key(tenant_id))
        if raw:
            return max(int(raw) - _OVERLAP_SECONDS, now - _LOOKBACK_SECONDS)
    except Exception:
        pass
    return now - _LOOKBACK_SECONDS


def _write_since_ts(tenant_id: uuid.UUID, ts: int) -> None:
    try:
        get_redis().set(_since_key(tenant_id), str(ts), ex=86400)
    except Exception:
        pass


def pull_recent_evolution_messages(tenant_id: uuid.UUID) -> int:
    """Importa mensajes nuevos desde Evolution DB (solo sin modo Chatwoot)."""
    from app.application.chatwoot.chatwoot_service import chatwoot_sync_mode

    if chatwoot_sync_mode():
        return 0
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Tenant, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus
    from app.infrastructure.evolution.evolution_store import fetch_recent_stored_messages
    from app.application.messaging.message_service import (
        parse_messages_upsert,
        save_inbound_message,
        save_outbound_from_phone,
    )
    from app.application.messaging.webhook_processor import _commit_and_publish_message
    from app.application.workers.queue_service import is_duplicate_webhook

    if not settings.evolution_database_url:
        return 0

    db = SessionLocal()
    imported = 0
    max_ts = 0
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        session = (
            db.query(WhatsAppSession)
            .filter(WhatsAppSession.tenant_id == tenant_id)
            .first()
        )
        if (
            tenant is None
            or session is None
            or tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value
            or session.active_connection_id is None
        ):
            return 0

        since_ts = _read_since_ts(tenant_id)
        records = fetch_recent_stored_messages(
            settings.evolution_database_url,
            session.instance_name,
            since_ts=since_ts,
            limit=150,
        )
        if not records:
            return 0

        connection_id = session.active_connection_id
        pending_ai: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []

        for item in parse_messages_upsert(records):
            msg_id = item.get("message_id") or ""
            if msg_id and is_duplicate_webhook(tenant_id, f"messages.upsert:{msg_id}"):
                continue

            ts_raw = next(
                (
                    r.get("messageTimestamp")
                    for r in records
                    if str((r.get("key") or {}).get("id") or "") == msg_id
                ),
                None,
            )
            if ts_raw:
                try:
                    max_ts = max(max_ts, int(ts_raw))
                except (TypeError, ValueError):
                    pass

            try:
                if item.get("from_me"):
                    msg = save_outbound_from_phone(
                        db,
                        tenant=tenant,
                        evolution_message_id=item["message_id"],
                        remote_jid=item["remote_jid"],
                        body=item["body"],
                        message_key=item.get("key") if isinstance(item.get("key"), dict) else None,
                        lid_jid=item.get("lid_jid") or "",
                        whatsapp_connection_id=connection_id,
                        instance_name=session.instance_name,
                        publish=False,
                    )
                    event_type = "message.out"
                else:
                    msg = save_inbound_message(
                        db,
                        tenant=tenant,
                        evolution_message_id=item["message_id"],
                        remote_jid=item["remote_jid"],
                        body=item["body"],
                        push_name=item.get("push_name") or "",
                        message_key=item.get("key") if isinstance(item.get("key"), dict) else None,
                        lid_jid=item.get("lid_jid") or "",
                        whatsapp_connection_id=connection_id,
                        instance_name=session.instance_name,
                        publish=False,
                    )
                    event_type = "message.in"

                if msg is not None:
                    _commit_and_publish_message(
                        db,
                        tenant=tenant,
                        message=msg,
                        event_type=event_type,
                    )
                    imported += 1
                    if event_type == "message.in":
                        pending_ai.append((tenant.id, msg.conversation_id, msg.id))
            except Exception:
                log.exception("live_pull msg=%s tenant=%s", msg_id, tenant_id)
                db.rollback()

        if pending_ai:
            from app.application.ai.ai_queue_service import flush_pending_ai_replies

            flush_pending_ai_replies(pending_ai)

        if max_ts:
            _write_since_ts(tenant_id, max_ts)
        elif imported:
            _write_since_ts(
                tenant_id, int(datetime.now(timezone.utc).timestamp())
            )

        if imported:
            from app.application.sync.chat_sync_service import _recompute_last_message_at

            _recompute_last_message_at(db, tenant_id=tenant_id)
            db.commit()

        return imported
    except Exception:
        log.exception("pull_recent_evolution_messages tenant=%s", tenant_id)
        db.rollback()
        return imported
    finally:
        db.close()


def _scheduler_loop() -> None:
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Tenant
    from app.domain.entities.enums import WhatsAppStatus

    log.info("Live pull scheduler started (every %.0fs)", _POLL_SECONDS)
    while not _scheduler_stop.is_set():
        try:
            with SessionLocal() as db:
                tenants = (
                    db.query(Tenant)
                    .filter(Tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value)
                    .all()
                )
                tenant_ids = [t.id for t in tenants]
            for tid in tenant_ids:
                if _scheduler_stop.is_set():
                    break
                n = pull_recent_evolution_messages(tid)
                if n:
                    log.info("live_pull tenant=%s imported=%s", tid, n)
        except Exception:
            log.exception("live_pull scheduler tick")
        _scheduler_stop.wait(_POLL_SECONDS)
    log.info("Live pull scheduler stopped")


def start_live_pull_scheduler() -> None:
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        name="live-pull-scheduler",
        daemon=True,
    )
    _scheduler_thread.start()


def stop_live_pull_scheduler() -> None:
    _scheduler_stop.set()
    if _scheduler_thread and _scheduler_thread.is_alive():
        _scheduler_thread.join(timeout=2)


def live_pull_scheduler_is_alive() -> bool:
    return bool(_scheduler_thread and _scheduler_thread.is_alive())

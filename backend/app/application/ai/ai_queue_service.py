from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import ALLOWED_DEEPSEEK_MODEL, settings
from app.infrastructure.persistence.database import SessionLocal
from app.domain.entities import Conversation, Tenant
from app.infrastructure.cache.redis_client import get_redis
from app.application.ai.ai_service import ai_block_reason, should_ai_respond
from app.infrastructure.ai.deepseek_client import is_configured
from app.application.workers.worker_runtime import (
    acquire_ai_slot,
    acquire_tenant_ai_slot,
    refresh_ai_slot,
    release_ai_slot,
    release_tenant_ai_slot,
)

log = logging.getLogger(__name__)

AI_REPLY_QUEUE = "queue:ai_replies"
# Reintentos con fecha (score = epoch en que toca): la IA no respondió y se vuelve a intentar.
AI_DELAYED_QUEUE = "queue:ai_replies:delayed"
DELAYED_PROMOTE_INTERVAL_SECONDS = 2.0
AI_PROCESS_LOCK_PREFIX = "ai:lock:"
AI_PROCESS_LOCK_TTL_SECONDS = 120
AI_QUEUED_PREFIX = "ai:queued:"
AI_QUEUED_TTL_SECONDS = 600
MAX_AI_JOB_RETRIES = 8
TENANT_BUSY_REQUEUE_DELAY_SECONDS = 0.25
JOB_REPLY = "reply"
JOB_RESCUE = "rescue"  # el chat pasó a una persona: revisar si alguien le respondió al cliente

_worker_threads: list[threading.Thread] = []
_worker_stop = threading.Event()


def _ai_lock_key(conversation_id: uuid.UUID) -> str:
    return f"{AI_PROCESS_LOCK_PREFIX}{conversation_id}"


def _try_acquire_ai_lock(conversation_id: uuid.UUID) -> bool:
    return bool(
        get_redis().set(
            _ai_lock_key(conversation_id),
            "1",
            nx=True,
            ex=AI_PROCESS_LOCK_TTL_SECONDS,
        )
    )


def _release_ai_lock(conversation_id: uuid.UUID) -> None:
    try:
        get_redis().delete(_ai_lock_key(conversation_id))
    except Exception:
        pass


def _queued_key(message_id: uuid.UUID) -> str:
    return f"{AI_QUEUED_PREFIX}{message_id}"


def _clear_queued_marker(message_id: uuid.UUID) -> None:
    try:
        get_redis().delete(_queued_key(message_id))
    except Exception:
        pass


def _job_payload(
    tenant_id: uuid.UUID, conversation_id: uuid.UUID, message_id: uuid.UUID, kind: str
) -> dict:
    payload = {
        "tenant_id": str(tenant_id),
        "conversation_id": str(conversation_id),
        "message_id": str(message_id),
    }
    if kind != JOB_REPLY:
        payload["kind"] = kind
    return payload


def _process_ai_job(
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    kind: str = JOB_REPLY,
) -> None:
    if not _try_acquire_ai_lock(conversation_id):
        if kind == JOB_RESCUE:
            # Otro job tiene el chat: el rescate no se puede perder, se intenta en un rato.
            schedule_ai_retry(tenant_id, conversation_id, message_id, delay_seconds=15, kind=kind)
            return
        log.debug("IA job omitido: lock activo conv=%s", conversation_id)
        _clear_queued_marker(message_id)
        return

    tenant_slot = acquire_tenant_ai_slot(tenant_id)
    if tenant_slot is None:
        log.debug("IA: negocio en su tope de respuestas simultáneas tenant=%s (reencolar)", tenant_id)
        _release_ai_lock(conversation_id)
        time.sleep(TENANT_BUSY_REQUEUE_DELAY_SECONDS)
        _requeue_job(_job_payload(tenant_id, conversation_id, message_id, kind), retry=0)
        return

    slot = acquire_ai_slot(wait_seconds=settings.ai_slot_wait_seconds)
    if slot is None:
        log.warning(
            "IA sin slot disponible conv=%s msg=%s (reencolar)",
            conversation_id,
            message_id,
        )
        release_tenant_ai_slot(tenant_slot)
        _release_ai_lock(conversation_id)
        _requeue_job(_job_payload(tenant_id, conversation_id, message_id, kind), retry=1)
        return

    from app.application.ai.ai_service import process_ai_reply, rescue_unanswered_handoff

    handler = rescue_unanswered_handoff if kind == JOB_RESCUE else process_ai_reply
    db = SessionLocal()
    try:
        refresh_ai_slot(slot)
        handler(
            db,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        db.commit()
    except Exception:
        log.exception("AI job error conv=%s msg=%s", conversation_id, message_id)
        db.rollback()
    finally:
        db.close()
        release_ai_slot(slot)
        release_tenant_ai_slot(tenant_slot)
        _release_ai_lock(conversation_id)
        _clear_queued_marker(message_id)


def is_fresh_for_ai(created_at: Optional[datetime]) -> bool:
    """Tras una caída llegan mensajes de hace horas: contestarlos ahora confunde al cliente."""
    if created_at is None:
        return True
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - created_at
    return age <= timedelta(minutes=settings.ai_max_reply_age_minutes)


def enqueue_ai_reply_ids(
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    *,
    db: Optional["Session"] = None,
    allow_stale: bool = False,
) -> bool:
    """Encola respuesta IA. Llamar solo después de commit del mensaje entrante.

    `allow_stale` solo para cuando el dueño pide la respuesta a mano desde el panel."""
    from sqlalchemy.orm import Session

    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        conversation = (
            db.query(Conversation)
            .filter(
                Conversation.id == conversation_id,
                Conversation.tenant_id == tenant_id,
            )
            .first()
        )
        if tenant is None or conversation is None:
            return False
        if not should_ai_respond(tenant, conversation):
            reason = ai_block_reason(tenant, conversation) or "desconocido"
            log.info("IA no encolada conv=%s: %s", conversation_id, reason)
            from app.application.ai.ai_service import schedule_rescue_if_awaiting

            schedule_rescue_if_awaiting(tenant_id, conversation_id, message_id)
            return False
        if not allow_stale:
            from app.domain.entities import Message

            created_at = (
                db.query(Message.created_at)
                .filter(Message.id == message_id, Message.tenant_id == tenant_id)
                .scalar()
            )
            if not is_fresh_for_ai(created_at):
                log.info(
                    "IA no encolada conv=%s: mensaje de hace más de %s min",
                    conversation_id,
                    settings.ai_max_reply_age_minutes,
                )
                return False
    finally:
        if owns_session:
            db.close()

    if not get_redis().set(_queued_key(message_id), "1", nx=True, ex=AI_QUEUED_TTL_SECONDS):
        log.debug("IA ya encolada msg=%s", message_id)
        return True

    item = json.dumps(
        {
            "tenant_id": str(tenant_id),
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
            "retry": 0,
        }
    )
    get_redis().lpush(AI_REPLY_QUEUE, item)
    log.info("IA encolada conv=%s msg=%s", conversation_id, message_id)
    return True


def flush_pending_ai_replies(
    jobs: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]],
) -> None:
    for tenant_id, conversation_id, message_id in jobs:
        enqueue_ai_reply_ids(tenant_id, conversation_id, message_id)


def _requeue_job(data: dict, retry: int) -> None:
    payload = dict(data)
    payload["retry"] = retry
    get_redis().lpush(AI_REPLY_QUEUE, json.dumps(payload))


def schedule_ai_retry(
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    *,
    delay_seconds: float,
    kind: str = JOB_REPLY,
) -> None:
    item = json.dumps({**_job_payload(tenant_id, conversation_id, message_id, kind), "retry": 0})
    get_redis().zadd(AI_DELAYED_QUEUE, {item: time.time() + delay_seconds})
    log.info("IA %s en %.0fs conv=%s msg=%s", kind, delay_seconds, conversation_id, message_id)


def promote_due_ai_retries(now: Optional[float] = None) -> int:
    """Pasa a la cola normal los reintentos que ya vencieron. Seguro con varios workers:
    solo quien logra el ZREM encola el job."""
    client = get_redis()
    due = client.zrangebyscore(AI_DELAYED_QUEUE, "-inf", now or time.time(), start=0, num=100)
    promoted = 0
    for raw in due:
        if client.zrem(AI_DELAYED_QUEUE, raw):
            client.lpush(AI_REPLY_QUEUE, raw)
            promoted += 1
    return promoted


def _worker_loop() -> None:
    log.info("AI worker started")
    last_promote = 0.0
    while not _worker_stop.is_set():
        try:
            if time.monotonic() - last_promote >= DELAYED_PROMOTE_INTERVAL_SECONDS:
                last_promote = time.monotonic()
                promote_due_ai_retries()
            item = get_redis().brpop(AI_REPLY_QUEUE, timeout=2)
            if not item:
                continue
            _, raw = item
            data = json.loads(raw)
            retry = int(data.get("retry") or 0)
            tenant_id = uuid.UUID(data["tenant_id"])
            conversation_id = uuid.UUID(data["conversation_id"])
            message_id = uuid.UUID(data["message_id"])

            db = SessionLocal()
            try:
                from app.domain.entities import Message

                found = (
                    db.query(Message.id)
                    .filter(
                        Message.id == message_id,
                        Message.conversation_id == conversation_id,
                        Message.tenant_id == tenant_id,
                    )
                    .first()
                )
                if found is None and retry < MAX_AI_JOB_RETRIES:
                    time.sleep(0.3 * (retry + 1))
                    _requeue_job(data, retry + 1)
                    continue
            finally:
                db.close()

            _process_ai_job(tenant_id, conversation_id, message_id, data.get("kind") or JOB_REPLY)
        except Exception:
            log.exception("AI worker error")
    log.info("AI worker stopped")


def recover_pending_ai_replies() -> None:
    """Al arrancar, reencola chats con mensaje entrante sin respuesta bot."""

    def _run() -> None:
        time.sleep(3)
        from app.domain.entities import Conversation, Tenant
        from app.domain.entities.enums import ConversationMode, ConversationStatus, WhatsAppStatus
        from app.application.ai.ai_service import maybe_schedule_ai_for_conversation

        db = SessionLocal()
        try:
            tenants = db.query(Tenant).filter(Tenant.ai_global_enabled.is_(True)).all()
            scheduled = 0
            for tenant in tenants:
                if tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value:
                    continue
                conversations = (
                    db.query(Conversation)
                    .filter(
                        Conversation.tenant_id == tenant.id,
                        Conversation.ai_active.is_(True),
                        Conversation.mode == ConversationMode.AUTO.value,
                        Conversation.status != ConversationStatus.EXCLUDED.value,
                    )
                    .all()
                )
                for conversation in conversations:
                    if maybe_schedule_ai_for_conversation(
                        db,
                        tenant_id=tenant.id,
                        conversation_id=conversation.id,
                    ):
                        scheduled += 1
            if scheduled:
                log.info("IA recovery: %s conversación(es) reencolada(s)", scheduled)
        except Exception:
            log.exception("IA recovery error")
        finally:
            db.close()

    threading.Thread(target=_run, name="ai-recovery", daemon=True).start()


def start_ai_worker() -> None:
    global _worker_threads
    if any(t.is_alive() for t in _worker_threads):
        return
    _worker_stop.clear()
    # Cada respuesta pasa varios segundos esperando a DeepSeek: un solo hilo atendería a toda
    # la plataforma de a una. Los slots globales y por negocio siguen limitando el total.
    _worker_threads = [
        threading.Thread(target=_worker_loop, name=f"ai-worker-{n}", daemon=True)
        for n in range(max(1, settings.ai_worker_threads))
    ]
    for thread in _worker_threads:
        thread.start()
    from app.application.conversations.interest_alert_service import start_interest_alert_sweeper
    from app.application.whatsapp.whatsapp_reconnect_service import start_whatsapp_reconnect_watchdog

    start_interest_alert_sweeper()
    start_whatsapp_reconnect_watchdog()
    if is_configured():
        log.info(
            "AI worker started (model=%s, threads=%s, max_parallel=%s, per_tenant=%s)",
            ALLOWED_DEEPSEEK_MODEL,
            len(_worker_threads),
            settings.ai_max_parallel_jobs,
            settings.ai_max_parallel_per_tenant,
        )
    else:
        log.warning("AI worker started sin DEEPSEEK_API_KEY — solo respuestas fallback")


def stop_ai_worker() -> None:
    from app.application.conversations.interest_alert_service import stop_interest_alert_sweeper
    from app.application.whatsapp.whatsapp_reconnect_service import stop_whatsapp_reconnect_watchdog

    stop_interest_alert_sweeper()
    stop_whatsapp_reconnect_watchdog()
    _worker_stop.set()
    for thread in _worker_threads:
        if thread.is_alive():
            thread.join(timeout=1)


def ai_worker_is_alive() -> bool:
    return any(t.is_alive() for t in _worker_threads)

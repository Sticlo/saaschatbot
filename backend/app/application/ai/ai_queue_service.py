from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Optional

from app.config import ALLOWED_DEEPSEEK_MODEL, settings
from app.infrastructure.persistence.database import SessionLocal
from app.domain.entities import Conversation, Tenant
from app.infrastructure.cache.redis_client import get_redis
from app.application.ai.ai_service import ai_block_reason, should_ai_respond
from app.infrastructure.ai.deepseek_client import is_configured
from app.application.workers.worker_runtime import (
    acquire_ai_slot,
    refresh_ai_slot,
    release_ai_slot,
)

log = logging.getLogger(__name__)

AI_REPLY_QUEUE = "queue:ai_replies"
AI_PROCESS_LOCK_PREFIX = "ai:lock:"
AI_PROCESS_LOCK_TTL_SECONDS = 120
MAX_AI_JOB_RETRIES = 8

_worker_thread: Optional[threading.Thread] = None
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


def _process_ai_job(
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
) -> None:
    if not _try_acquire_ai_lock(conversation_id):
        log.debug("IA job omitido: lock activo conv=%s", conversation_id)
        return

    slot = acquire_ai_slot(wait_seconds=settings.ai_slot_wait_seconds)
    if slot is None:
        log.warning(
            "IA sin slot disponible conv=%s msg=%s (reencolar)",
            conversation_id,
            message_id,
        )
        _release_ai_lock(conversation_id)
        _requeue_job(
            {
                "tenant_id": str(tenant_id),
                "conversation_id": str(conversation_id),
                "message_id": str(message_id),
                "retry": 0,
            },
            retry=1,
        )
        return

    from app.application.ai.ai_service import process_ai_reply

    db = SessionLocal()
    try:
        refresh_ai_slot(slot)
        process_ai_reply(
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
        _release_ai_lock(conversation_id)


def enqueue_ai_reply_ids(
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
) -> None:
    """Encola respuesta IA. Llamar solo después de commit del mensaje entrante."""
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
            return
        if not should_ai_respond(tenant, conversation):
            reason = ai_block_reason(tenant, conversation) or "desconocido"
            log.info("IA no encolada conv=%s: %s", conversation_id, reason)
            return
    finally:
        db.close()

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


def flush_pending_ai_replies(
    jobs: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]],
) -> None:
    for tenant_id, conversation_id, message_id in jobs:
        enqueue_ai_reply_ids(tenant_id, conversation_id, message_id)


def _requeue_job(data: dict, retry: int) -> None:
    payload = dict(data)
    payload["retry"] = retry
    get_redis().lpush(AI_REPLY_QUEUE, json.dumps(payload))


def _worker_loop() -> None:
    log.info("AI worker started")
    while not _worker_stop.is_set():
        try:
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

            _process_ai_job(tenant_id, conversation_id, message_id)
        except Exception:
            log.exception("AI worker error")
    log.info("AI worker stopped")


def recover_pending_ai_replies() -> None:
    """Al arrancar, reencola chats con mensaje entrante sin respuesta bot."""

    def _run() -> None:
        time.sleep(3)
        from app.domain.entities import Conversation, Tenant
        from app.domain.entities.enums import ConversationMode, ConversationStatus
        from app.application.ai.ai_service import maybe_schedule_ai_for_conversation

        db = SessionLocal()
        try:
            tenants = db.query(Tenant).filter(Tenant.ai_global_enabled.is_(True)).all()
            scheduled = 0
            for tenant in tenants:
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
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        name="ai-worker",
        daemon=True,
    )
    _worker_thread.start()
    if is_configured():
        log.info(
            "AI worker started (model=%s, max_parallel=%s)",
            ALLOWED_DEEPSEEK_MODEL,
            settings.ai_max_parallel_jobs,
        )
    else:
        log.warning("AI worker started sin DEEPSEEK_API_KEY — solo respuestas fallback")


def stop_ai_worker() -> None:
    _worker_stop.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=5)


def ai_worker_is_alive() -> bool:
    return bool(_worker_thread and _worker_thread.is_alive())

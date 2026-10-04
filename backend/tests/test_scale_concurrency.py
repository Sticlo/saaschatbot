"""Carga y concurrencia: muchos negocios activos a la vez (objetivo: 120+), carreras al
crear chats, reparto justo de la IA entre negocios y respuestas a mensajes viejos."""
from __future__ import annotations

import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from app.application.messaging.webhook_processor import process_evolution_webhook
from app.domain.entities import Conversation, Message, Tenant
from app.domain.entities.enums import (
    ConversationMode,
    MessageDirection,
    MessageSource,
    MessageStatus,
)
from tests.conftest import make_wa_tenant, requires_db, upsert_payload


def _msg_id() -> str:
    return f"3EB0{uuid.uuid4().hex[:16].upper()}"


def _add_conversation(db, tenant_id, connection_id, phone, *, inbound_at=None, body="hola"):
    conversation = Conversation(
        tenant_id=tenant_id,
        contact_phone=phone,
        contact_name="Cliente",
        whatsapp_connection_id=connection_id,
        ai_active=True,
        mode=ConversationMode.AUTO.value,
    )
    db.add(conversation)
    db.flush()
    message = Message(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        direction=MessageDirection.IN.value,
        source=MessageSource.CONTACT.value,
        body=body,
        status=MessageStatus.RECEIVED.value,
        created_at=inbound_at or datetime.now(timezone.utc),
    )
    db.add(message)
    db.flush()
    return conversation, message


def _enable_ai(db, tenant_id):
    db.query(Tenant).filter(Tenant.id == tenant_id).update({"ai_global_enabled": True})


# ── Muchos negocios a la vez ────────────────────────────────────────────────


@requires_db
def test_forty_businesses_receiving_at_once_keep_their_messages_apart(wa_offline):
    """40 negocios × 5 mensajes llegando intercalados por 16 hilos: ni pérdidas, ni
    duplicados, ni mensajes cruzados de un negocio a otro."""
    from app.infrastructure.persistence.database import SessionLocal

    tenants = []
    with SessionLocal() as db:
        for n in range(40):
            tenants.append(make_wa_tenant(db, label=f"Carga{n}"))

    jobs = []
    for n, (tenant, wa) in enumerate(tenants):
        for k in range(5):
            payload = upsert_payload(
                wa.instance_name,
                msg_id=_msg_id(),
                text=f"negocio-{n} mensaje-{k}",
                phone=f"5730{n:02d}{k:06d}",
            )
            jobs.append((tenant.id, payload))
    random.Random(7).shuffle(jobs)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda job: process_evolution_webhook(*job), jobs))
    elapsed = time.perf_counter() - started
    print(f"\n{len(jobs)} webhooks de 40 negocios en {elapsed:.2f}s ({len(jobs) / elapsed:.0f} msg/s)")

    with SessionLocal() as db:
        for n, (tenant, _wa) in enumerate(tenants):
            bodies = sorted(
                b for (b,) in db.query(Message.body).filter(Message.tenant_id == tenant.id).all()
            )
            assert bodies == sorted(f"negocio-{n} mensaje-{k}" for k in range(5))
            assert db.query(Conversation).filter(Conversation.tenant_id == tenant.id).count() == 5
    assert len(wa_offline["ai_jobs"]) == len(jobs)


@requires_db
def test_first_messages_of_a_new_contact_arriving_together_share_one_chat(wa_offline):
    """Un cliente nuevo manda 8 mensajes seguidos y cada uno lo procesa un hilo distinto:
    todos quedan en un solo chat y el contador de no leídos no pierde ninguno."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)

    gate = threading.Barrier(8)

    def send(k: int) -> None:
        payload = upsert_payload(wa.instance_name, msg_id=_msg_id(), text=f"parte {k}", phone="573209998877")
        gate.wait()
        process_evolution_webhook(tenant.id, payload)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(send, range(8)))

    with SessionLocal() as db:
        [conv] = db.query(Conversation).filter(Conversation.tenant_id == tenant.id).all()
        assert db.query(Message).filter(Message.conversation_id == conv.id).count() == 8
        assert conv.unread_count == 8


@requires_db
def test_chat_created_by_another_process_in_between_is_reused_not_lost(monkeypatch):
    """Carrera determinista: otro proceso crea el chat justo entre la búsqueda y el INSERT.
    Antes la restricción única tumbaba la transacción y el mensaje del cliente se perdía."""
    from app.application.messaging import message_service
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
        winner = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573005556677",
            contact_name="Cliente",
            whatsapp_connection_id=wa.active_connection_id,
        )
        db.add(winner)
        db.commit()
        winner_id = winner.id

    real_find = message_service.find_conversation_for_contact
    calls = {"n": 0}

    def find_misses_first_time(*args, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(*args, **kwargs)

    monkeypatch.setattr(message_service, "find_conversation_for_contact", find_misses_first_time)

    with SessionLocal() as db:
        conversation = message_service.get_or_create_conversation(
            db,
            tenant_id=tenant.id,
            contact_phone="+573005556677",
            whatsapp_connection_id=wa.active_connection_id,
        )
        assert conversation.id == winner_id
        db.add(
            Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                direction=MessageDirection.IN.value,
                source=MessageSource.CONTACT.value,
                body="sigo aquí",
                status=MessageStatus.RECEIVED.value,
            )
        )
        db.commit()

    with SessionLocal() as db:
        assert db.query(Conversation).filter(Conversation.tenant_id == tenant.id).count() == 1
        assert db.query(Message).filter(Message.conversation_id == winner_id).count() == 1


@requires_db
def test_new_contact_lookup_does_not_load_every_chat_of_a_big_business(wa_offline):
    """Un negocio con 300 chats: un contacto nuevo no debe cargar los 300 en memoria."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
        db.add_all(
            Conversation(
                tenant_id=tenant.id,
                contact_phone=f"+57311{i:07d}",
                contact_name=f"Cliente {i}",
                whatsapp_connection_id=wa.active_connection_id,
            )
            for i in range(300)
        )
        db.commit()

    loaded = {"n": 0}

    def on_load(target, _context):
        if target.tenant_id == tenant.id:
            loaded["n"] += 1

    event.listen(Conversation, "load", on_load)
    try:
        process_evolution_webhook(
            tenant.id, upsert_payload(wa.instance_name, msg_id=_msg_id(), text="hola", phone="573129990000")
        )
    finally:
        event.remove(Conversation, "load", on_load)

    assert loaded["n"] < 10, f"se cargaron {loaded['n']} chats para un contacto nuevo"


@requires_db
def test_sql_fallback_still_matches_other_phone_formats_and_lid_placeholders():
    from app.application.messaging.message_service import find_conversation_for_contact
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
        local = Conversation(
            tenant_id=tenant.id,
            contact_phone="300 123 4567",
            whatsapp_connection_id=wa.active_connection_id,
        )
        lid = Conversation(
            tenant_id=tenant.id,
            contact_phone="lid:998877665544",
            whatsapp_connection_id=wa.active_connection_id,
        )
        db.add_all([local, lid])
        db.flush()

        assert find_conversation_for_contact(
            db,
            tenant_id=tenant.id,
            whatsapp_connection_id=wa.active_connection_id,
            contact_phone="+573001234567",
        ).id == local.id
        assert find_conversation_for_contact(
            db,
            tenant_id=tenant.id,
            whatsapp_connection_id=wa.active_connection_id,
            contact_jid="998877665544@lid",
        ).id == lid.id
        db.rollback()


# ── Webhook ─────────────────────────────────────────────────────────────────


@requires_db
def test_webhook_without_instance_is_rejected(wa_offline):
    """El secreto es el mismo para todos los negocios: sin instancia no hay forma de saber
    que el payload es de este negocio."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
    payload = upsert_payload(wa.instance_name, msg_id=_msg_id(), text="sin instancia")
    payload.pop("instance")

    process_evolution_webhook(tenant.id, payload)

    with SessionLocal() as db:
        assert db.query(Message).filter(Message.tenant_id == tenant.id).count() == 0
    assert wa_offline["ai_jobs"] == []


# ── IA: mensajes viejos ─────────────────────────────────────────────────────


@requires_db
def test_ai_skips_messages_older_than_the_limit_unless_owner_asks(monkeypatch):
    from app.application.ai import ai_queue_service as queue
    from app.config import settings
    from app.infrastructure.cache.redis_client import get_redis
    from app.infrastructure.persistence.database import SessionLocal

    test_queue = f"test:ai:{uuid.uuid4().hex}"
    monkeypatch.setattr(queue, "AI_REPLY_QUEUE", test_queue)
    monkeypatch.setattr(settings, "ai_max_reply_age_minutes", 120)

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
        _enable_ai(db, tenant.id)
        old_conv, old_msg = _add_conversation(
            db, tenant.id, wa.active_connection_id, "+573001110001",
            inbound_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        new_conv, new_msg = _add_conversation(db, tenant.id, wa.active_connection_id, "+573001110002")
        db.commit()

        try:
            assert queue.enqueue_ai_reply_ids(tenant.id, old_conv.id, old_msg.id, db=db) is False
            assert queue.enqueue_ai_reply_ids(tenant.id, new_conv.id, new_msg.id, db=db) is True
            assert get_redis().llen(test_queue) == 1
            assert queue.enqueue_ai_reply_ids(
                tenant.id, old_conv.id, old_msg.id, db=db, allow_stale=True
            ) is True
            assert get_redis().llen(test_queue) == 2
        finally:
            get_redis().delete(test_queue)
            queue._clear_queued_marker(old_msg.id)
            queue._clear_queued_marker(new_msg.id)


def test_reply_age_limit_handles_naive_and_missing_timestamps(monkeypatch):
    from app.application.ai.ai_queue_service import is_fresh_for_ai
    from app.config import settings

    monkeypatch.setattr(settings, "ai_max_reply_age_minutes", 120)
    now = datetime.now(timezone.utc)
    assert is_fresh_for_ai(None)
    assert is_fresh_for_ai(now - timedelta(minutes=119))
    assert not is_fresh_for_ai(now - timedelta(minutes=121))
    assert not is_fresh_for_ai((now - timedelta(hours=5)).replace(tzinfo=None))


# ── IA: reparto justo y paralelismo ─────────────────────────────────────────


@requires_db
def test_one_business_cannot_take_more_than_its_ai_slots(monkeypatch):
    from app.application.workers.worker_runtime import (
        acquire_tenant_ai_slot,
        release_tenant_ai_slot,
    )
    from app.config import settings

    monkeypatch.setattr(settings, "ai_max_parallel_per_tenant", 3)
    busy, quiet = uuid.uuid4(), uuid.uuid4()

    taken = [acquire_tenant_ai_slot(busy) for _ in range(3)]
    try:
        assert all(taken)
        assert acquire_tenant_ai_slot(busy) is None
        other = acquire_tenant_ai_slot(quiet)
        assert other is not None
        release_tenant_ai_slot(other)

        release_tenant_ai_slot(taken.pop())
        again = acquire_tenant_ai_slot(busy)
        assert again is not None
        taken.append(again)
    finally:
        for key in taken:
            release_tenant_ai_slot(key)


@requires_db
def test_ai_workers_answer_in_parallel_and_a_busy_business_does_not_block_others(monkeypatch):
    """Negocio A tiene 10 mensajes en cola delante del único mensaje del negocio B.
    Con varios hilos y tope por negocio, B arranca sin esperar a que A termine todo."""
    from app.application.ai import ai_queue_service as queue
    from app.application.ai import ai_service
    from app.application.conversations import interest_alert_service
    from app.application.whatsapp import whatsapp_reconnect_service
    from app.config import settings
    from app.infrastructure.cache.redis_client import get_redis
    from app.infrastructure.persistence.database import SessionLocal

    queue.stop_ai_worker()
    for thread in list(queue._worker_threads):
        thread.join(timeout=3)

    test_queue = f"test:ai:{uuid.uuid4().hex}"
    monkeypatch.setattr(queue, "AI_REPLY_QUEUE", test_queue)
    monkeypatch.setattr(queue, "TENANT_BUSY_REQUEUE_DELAY_SECONDS", 0.05)
    monkeypatch.setattr(settings, "ai_worker_threads", 4)
    monkeypatch.setattr(settings, "ai_max_parallel_per_tenant", 2)
    for module, name in (
        (interest_alert_service, "start_interest_alert_sweeper"),
        (interest_alert_service, "stop_interest_alert_sweeper"),
        (whatsapp_reconnect_service, "start_whatsapp_reconnect_watchdog"),
        (whatsapp_reconnect_service, "stop_whatsapp_reconnect_watchdog"),
    ):
        monkeypatch.setattr(module, name, lambda: None)

    jobs = []
    with SessionLocal() as db:
        busy, busy_wa = make_wa_tenant(db, label="Busy")
        quiet, quiet_wa = make_wa_tenant(db, label="Quiet")
        for i in range(10):
            conv, msg = _add_conversation(db, busy.id, busy_wa.active_connection_id, f"+57300200{i:04d}")
            jobs.append((busy.id, conv.id, msg.id))
        conv, msg = _add_conversation(db, quiet.id, quiet_wa.active_connection_id, "+573003009999")
        jobs.append((quiet.id, conv.id, msg.id))
        db.commit()

    lock = threading.Lock()
    running: dict[uuid.UUID, int] = {}
    stats = {"max_busy": 0, "max_total": 0}
    started_at: dict[uuid.UUID, float] = {}
    done: list[uuid.UUID] = []

    def fake_reply(_db, *, tenant_id, conversation_id, message_id):
        with lock:
            started_at[message_id] = time.monotonic()
            running[tenant_id] = running.get(tenant_id, 0) + 1
            stats["max_busy"] = max(stats["max_busy"], running.get(busy.id, 0))
            stats["max_total"] = max(stats["max_total"], sum(running.values()))
        time.sleep(0.4)
        with lock:
            running[tenant_id] -= 1
            done.append(message_id)
        return True

    monkeypatch.setattr(ai_service, "process_ai_reply", fake_reply)

    client = get_redis()
    for tenant_id, conversation_id, message_id in jobs:
        client.lpush(
            test_queue,
            f'{{"tenant_id": "{tenant_id}", "conversation_id": "{conversation_id}", '
            f'"message_id": "{message_id}", "retry": 0}}',
        )

    t0 = time.monotonic()
    queue.start_ai_worker()
    try:
        deadline = time.monotonic() + 15
        while len(done) < len(jobs) and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        queue.stop_ai_worker()
        for thread in list(queue._worker_threads):
            thread.join(timeout=3)
        client.delete(test_queue)
    elapsed = time.monotonic() - t0

    assert sorted(map(str, done)) == sorted(str(j[2]) for j in jobs), "cada mensaje se responde una vez"
    assert stats["max_busy"] <= 2, "un negocio no supera su tope de respuestas simultáneas"
    assert stats["max_total"] >= 3, "los hilos responden en paralelo"
    quiet_msg = jobs[-1][2]
    last_busy_start = max(started_at[j[2]] for j in jobs[:-1])
    assert started_at[quiet_msg] < last_busy_start, "B no espera a que A vacíe su cola"
    print(f"\n11 respuestas IA (0.4s c/u) en {elapsed:.2f}s con 4 hilos")

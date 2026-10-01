from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tests.conftest import requires_db


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.queue: list[str] = []

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, key):
        self.store.pop(key, None)

    def lpush(self, _name, item):
        self.queue.append(item)


class _FakeQuery:
    def __init__(self, row):
        self.row = row

    def filter(self, *_args):
        return self

    def first(self):
        return self.row


class _FakeDb:
    def __init__(self, tenant, conversation):
        self.rows = [tenant, conversation]

    def query(self, _model):
        return _FakeQuery(self.rows.pop(0))


def test_same_message_is_enqueued_only_once(monkeypatch):
    from app.application.ai import ai_queue_service as queue

    redis = _FakeRedis()
    monkeypatch.setattr(queue, "get_redis", lambda: redis)
    monkeypatch.setattr(queue, "should_ai_respond", lambda *_: True)

    tenant_id, conversation_id, message_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        queue.enqueue_ai_reply_ids(
            tenant_id,
            conversation_id,
            message_id,
            db=_FakeDb(SimpleNamespace(), SimpleNamespace()),
        )
    assert len(redis.queue) == 1

    queue._clear_queued_marker(message_id)
    queue.enqueue_ai_reply_ids(
        tenant_id,
        conversation_id,
        message_id,
        db=_FakeDb(SimpleNamespace(), SimpleNamespace()),
    )
    assert len(redis.queue) == 2


@requires_db
def test_process_ai_reply_skips_message_already_answered(monkeypatch):
    from app.application.ai import ai_service
    from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
    from app.domain.entities.enums import (
        ConversationMode,
        MessageDirection,
        MessageSource,
        MessageStatus,
        WhatsAppStatus,
    )
    from app.infrastructure.persistence.database import SessionLocal

    def _fail(*_args, **_kwargs):
        raise AssertionError("no debe generar una segunda respuesta")

    monkeypatch.setattr(ai_service, "_process_full_reply", _fail)
    monkeypatch.setattr(ai_service, "_process_qualify", _fail)
    monkeypatch.setattr(ai_service.time, "sleep", _fail)

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Hotel Duplicado",
            slug=f"hotel-dup-{uuid.uuid4().hex[:8]}",
            whatsapp_status=WhatsAppStatus.CONNECTED.value,
            ai_global_enabled=True,
        )
        db.add(tenant)
        db.flush()
        connection_id = uuid.uuid4()
        db.add(
            WhatsAppSession(
                tenant_id=tenant.id,
                instance_name=f"inst_{uuid.uuid4().hex[:8]}",
                status=WhatsAppStatus.CONNECTED.value,
                active_connection_id=connection_id,
            )
        )
        conversation = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573004583560",
            contact_name="Angie",
            whatsapp_connection_id=connection_id,
            ai_active=True,
            mode=ConversationMode.AUTO.value,
        )
        db.add(conversation)
        db.flush()
        inbound = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            direction=MessageDirection.IN.value,
            source=MessageSource.CONTACT.value,
            body="Holi",
            status=MessageStatus.RECEIVED.value,
            created_at=now,
        )
        db.add(inbound)
        db.add(
            Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                direction=MessageDirection.OUT.value,
                source=MessageSource.BOT.value,
                body="¡Hola! ¿En qué te ayudo?",
                status=MessageStatus.SENT.value,
                created_at=now + timedelta(seconds=5),
            )
        )
        db.commit()

        assert (
            ai_service.process_ai_reply(
                db,
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                message_id=inbound.id,
            )
            is True
        )
        assert not ai_service.maybe_schedule_ai_for_conversation(
            db,
            tenant_id=tenant.id,
            conversation_id=conversation.id,
        )

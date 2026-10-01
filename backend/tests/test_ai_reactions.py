from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from tests.conftest import requires_db


def _setup(db, *, interest_status=None):
    from app.domain.entities import Conversation, Tenant, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus

    tenant = Tenant(
        business_name="Restaurante Brasa",
        slug=f"brasa-{uuid.uuid4().hex[:8]}",
        whatsapp_status=WhatsAppStatus.CONNECTED.value,
        ai_global_enabled=True,
    )
    db.add(tenant)
    db.flush()
    db.add(
        WhatsAppSession(
            tenant_id=tenant.id,
            instance_name=f"inst_{uuid.uuid4().hex[:8]}",
            status=WhatsAppStatus.CONNECTED.value,
        )
    )
    conversation = Conversation(
        tenant_id=tenant.id,
        contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
        contact_name="Cliente",
        ai_active=True,
        mode="auto",
        interest_status=interest_status,
    )
    db.add(conversation)
    db.flush()
    return tenant, conversation


def _add(db, conversation, history, *, offset: int = 0):
    """history: tuplas (source, body) en orden cronológico, a partir del minuto `offset`."""
    from app.domain.entities import Message
    from app.domain.entities.enums import MessageDirection

    start = datetime.now(timezone.utc) - timedelta(hours=1)
    rows = []
    for i, (source, body) in enumerate(history):
        row = Message(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            direction=MessageDirection.IN.value if source == "contact" else MessageDirection.OUT.value,
            source=source,
            body=body,
            status="received",
            created_at=start + timedelta(minutes=offset + i),
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


@requires_db
def test_reaction_after_question_does_not_hide_the_question(monkeypatch):
    from app.application.ai import ai_queue_service, ai_service
    from app.infrastructure.persistence.database import SessionLocal

    enqueued: list[uuid.UUID] = []
    monkeypatch.setattr(
        ai_queue_service, "enqueue_ai_reply_ids", lambda **kw: enqueued.append(kw["message_id"])
    )

    with SessionLocal() as db:
        tenant, conversation = _setup(db)
        question, reaction = _add(db, conversation, [("contact", "¿Cuánto vale la picada?"), ("contact", "👍")])

        assert ai_service._is_latest_inbound(db, conversation.id, question.id) is True
        assert ai_service._is_latest_inbound(db, conversation.id, reaction.id) is False
        assert ai_service.maybe_schedule_ai_for_conversation(
            db, tenant_id=tenant.id, conversation_id=conversation.id
        ) is True
        assert enqueued == [question.id]
        db.rollback()


@requires_db
def test_reaction_to_bot_reply_schedules_nothing(monkeypatch):
    from app.application.ai import ai_queue_service, ai_service
    from app.infrastructure.persistence.database import SessionLocal

    enqueued: list[uuid.UUID] = []
    monkeypatch.setattr(
        ai_queue_service, "enqueue_ai_reply_ids", lambda **kw: enqueued.append(kw["message_id"])
    )

    with SessionLocal() as db:
        tenant, conversation = _setup(db)
        _add(
            db,
            conversation,
            [("contact", "¿Abren hoy?"), ("bot", "Sí, de 12 a 10 pm"), ("contact", "❤️")],
        )
        assert ai_service.maybe_schedule_ai_for_conversation(
            db, tenant_id=tenant.id, conversation_id=conversation.id
        ) is False
        assert enqueued == []
        db.rollback()


@requires_db
def test_reaction_does_not_reopen_answered_interested_chat():
    from app.application.conversations.interest_alert_service import unanswered_interested_conversations
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conversation = _setup(db, interest_status="interested")
        _add(
            db,
            conversation,
            [("contact", "Quiero reservar mesa"), ("agent", "Listo, te esperamos"), ("contact", "❤️")],
        )
        assert unanswered_interested_conversations(db, tenant.id) == []

        _add(db, conversation, [("contact", "¿Puedo llevar torta?")], offset=10)
        assert [c.id for c in unanswered_interested_conversations(db, tenant.id)] == [conversation.id]
        db.rollback()

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import requires_db
from tests.test_phase1 import _register


def _setup(db, *, interest_status=None):
    from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
    from app.domain.entities.enums import MessageDirection, MessageSource, MessageStatus, WhatsAppStatus

    tenant = Tenant(
        business_name="Hotel Andino",
        slug=f"hotel-andino-{uuid.uuid4().hex[:8]}",
        whatsapp_status=WhatsAppStatus.CONNECTED.value,
        ai_global_enabled=True,
    )
    db.add(tenant)
    db.flush()
    connection_id = uuid.uuid4()
    session = WhatsAppSession(
        tenant_id=tenant.id,
        instance_name=f"inst_{uuid.uuid4().hex[:8]}",
        status=WhatsAppStatus.CONNECTED.value,
        active_connection_id=connection_id,
    )
    db.add(session)
    conversation = Conversation(
        tenant_id=tenant.id,
        contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
        contact_name="Cliente",
        whatsapp_connection_id=connection_id,
        ai_active=True,
        mode="auto",
        interest_status=interest_status,
    )
    db.add(conversation)
    db.flush()

    def message(body: str):
        row = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            direction=MessageDirection.IN.value,
            source=MessageSource.CONTACT.value,
            body=body,
            status=MessageStatus.RECEIVED.value,
        )
        db.add(row)
        db.flush()
        return row

    return tenant, session, conversation, message


def _close(db, tenant, session, conversation, msg, category):
    from app.application.ai import ai_service

    return ai_service._close_on_interest(
        db,
        tenant=tenant,
        conversation=conversation,
        session=session,
        message=msg,
        category=category,
    )


@requires_db
def test_purchase_intent_turns_ai_off_and_switches_to_manual(monkeypatch):
    from app.application.ai import ai_service
    from app.infrastructure.persistence.database import SessionLocal

    sent: list[str] = []
    monkeypatch.setattr(ai_service, "send_text_message", lambda *_a, **kw: sent.append(kw["text"]))
    monkeypatch.setattr(ai_service, "publish_conversation_updated", lambda *_a, **_kw: None)

    with SessionLocal() as db:
        tenant, session, conversation, message = _setup(db)
        msg = message("Quiero reservar una habitación para el sábado")
        assert _close(db, tenant, session, conversation, msg, "interesado") is True
        assert conversation.interest_status == "interested"
        assert conversation.ai_active is False
        assert conversation.mode == "manual"
        assert len(sent) == 1
        db.rollback()


@requires_db
def test_not_interested_turns_ai_off_and_switches_to_manual(monkeypatch):
    from app.application.ai import ai_service
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr(ai_service, "send_text_message", lambda *_a, **_kw: None)
    monkeypatch.setattr(ai_service, "publish_conversation_updated", lambda *_a, **_kw: None)

    with SessionLocal() as db:
        tenant, session, conversation, message = _setup(db)
        msg = message("No gracias, no me interesa")
        assert _close(db, tenant, session, conversation, msg, "no_interesado") is True
        assert conversation.interest_status == "not_interested"
        assert conversation.ai_active is False
        assert conversation.mode == "manual"
        db.rollback()


@requires_db
def test_casual_chat_keeps_ai_answering(monkeypatch):
    from app.application.ai import ai_service
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr(ai_service, "publish_conversation_updated", lambda *_a, **_kw: None)

    with SessionLocal() as db:
        tenant, session, conversation, message = _setup(db)
        for body, category in (("hola", "interesado"), ("Quiero jugar GTA 6", "interesado"), ("¿Cuánto vale la noche?", "duda")):
            assert _close(db, tenant, session, conversation, message(body), category) is False
        assert conversation.interest_status is None
        assert conversation.ai_active is True
        assert conversation.mode == "auto"
        db.rollback()


@requires_db
def test_after_agent_reenables_ai_only_label_updates(monkeypatch):
    from app.application.ai import ai_service
    from app.infrastructure.persistence.database import SessionLocal

    sent: list[str] = []
    monkeypatch.setattr(ai_service, "send_text_message", lambda *_a, **kw: sent.append(kw["text"]))
    monkeypatch.setattr(ai_service, "publish_conversation_updated", lambda *_a, **_kw: None)

    with SessionLocal() as db:
        tenant, session, conversation, message = _setup(db, interest_status="interested")
        msg = message("No me interesa ya")
        assert _close(db, tenant, session, conversation, msg, "no_interesado") is False
        assert conversation.interest_status == "not_interested"
        assert conversation.ai_active is True
        assert conversation.mode == "auto"
        assert sent == []
        db.rollback()


@requires_db
def test_marking_interest_from_panel_hands_chat_to_human(client: TestClient):
    from app.domain.entities import Conversation
    from app.infrastructure.persistence.database import SessionLocal

    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    with SessionLocal() as db:
        conversation = Conversation(
            tenant_id=uuid.UUID(auth["tenant_id"]),
            contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
            contact_name="Cliente",
            whatsapp_connection_id=uuid.uuid4(),
            ai_active=True,
        )
        db.add(conversation)
        db.commit()
        conversation_id = conversation.id

    for status in ("interested", "not_interested"):
        r = client.patch(
            f"/api/v1/conversations/{conversation_id}/interest",
            json={"interest_status": status},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["interest_status"] == status
        assert r.json()["ai_active"] is False
        assert r.json()["mode"] == "manual"

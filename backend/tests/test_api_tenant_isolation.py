"""Aislamiento por la API: con un token válido de otro negocio, ningún endpoint de
conversación deja ver ni modificar un chat ajeno (responde 404, como si no existiera)."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Conversation, Message
from app.domain.entities.enums import ConversationMode, MessageDirection, MessageSource, MessageStatus
from tests.conftest import requires_db


def _register(client: TestClient) -> dict:
    tag = uuid.uuid4().hex[:8]
    response = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Aislado {tag}",
            "owner_name": "Owner",
            "email": f"aislado-{tag}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def victim_and_intruder(client):
    from app.infrastructure.persistence.database import SessionLocal

    victim = _register(client)
    intruder = _register(client)
    with SessionLocal() as db:
        conversation = Conversation(
            tenant_id=uuid.UUID(victim["tenant_id"]),
            contact_phone="+573001234567",
            contact_name="Cliente de A",
            whatsapp_connection_id=uuid.uuid4(),
            ai_active=True,
            mode=ConversationMode.AUTO.value,
        )
        db.add(conversation)
        db.flush()
        message = Message(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            direction=MessageDirection.IN.value,
            source=MessageSource.CONTACT.value,
            body="dato privado de A",
            status=MessageStatus.RECEIVED.value,
        )
        db.add(message)
        db.commit()
        ids = (conversation.id, message.id)
    headers = {"Authorization": f"Bearer {intruder['access_token']}"}
    return ids, headers


def _requests(conversation_id, message_id):
    base = f"/api/v1/conversations/{conversation_id}"
    return [
        ("GET", f"{base}/messages", None),
        ("POST", f"{base}/messages", {"text": "hola desde B"}),
        ("POST", f"{base}/messages/shortcut", {"shortcut_id": "precios"}),
        ("POST", f"{base}/sync-live", None),
        ("PATCH", f"{base}/mode", {"mode": "manual"}),
        ("PATCH", f"{base}/ai", {"ai_active": False}),
        ("PATCH", f"{base}/interest", {"interest_status": "not_interested"}),
        ("POST", f"{base}/ai/trigger", None),
        ("GET", f"{base}/messages/{message_id}/media", None),
    ]


@requires_db
def test_another_business_gets_404_on_every_conversation_endpoint(client, victim_and_intruder):
    (conversation_id, message_id), headers = victim_and_intruder

    for method, url, body in _requests(conversation_id, message_id):
        response = client.request(method, url, json=body, headers=headers)
        assert response.status_code == 404, f"{method} {url} → {response.status_code} {response.text}"
        assert "dato privado" not in response.text


@requires_db
def test_failed_intrusion_leaves_the_chat_untouched(client, victim_and_intruder):
    from app.infrastructure.persistence.database import SessionLocal

    (conversation_id, message_id), headers = victim_and_intruder
    for method, url, body in _requests(conversation_id, message_id):
        client.request(method, url, json=body, headers=headers)

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation.mode == ConversationMode.AUTO.value
        assert conversation.ai_active is True
        assert conversation.interest_status is None
        assert db.query(Message).filter(Message.conversation_id == conversation_id).count() == 1


@requires_db
def test_conversation_list_only_shows_own_chats(client, victim_and_intruder):
    _ids, headers = victim_and_intruder
    response = client.get("/api/v1/conversations", headers=headers)
    assert response.status_code == 200, response.text
    assert "Cliente de A" not in response.text

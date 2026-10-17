"""Responder a un mensaje como en WhatsApp: se guarda a qué mensaje responde y se puede citar al enviar."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.application.ai.ai_conversation_service import format_history
from app.application.messaging.message_service import extract_quote, parse_messages_upsert
from tests.conftest import requires_db


def _upsert(message: dict, **extra) -> dict:
    return {
        "key": {"remoteJid": "573001112233@s.whatsapp.net", "fromMe": False, "id": "NEW-1"},
        "pushName": "Cliente",
        "message": message,
        **extra,
    }


def test_text_reply_keeps_the_quoted_message():
    parsed = parse_messages_upsert(_upsert({
        "extendedTextMessage": {
            "text": "Sí, esa talla",
            "contextInfo": {"stanzaId": "ORIG-9", "quotedMessage": {"conversation": "¿Tienes la 38?"}},
        }
    }))

    assert parsed[0]["body"] == "Sí, esa talla"
    assert parsed[0]["quoted_id"] == "ORIG-9"
    assert parsed[0]["quoted_body"] == "¿Tienes la 38?"


def test_photo_reply_to_a_photo_keeps_both():
    quote_id, quote_body = extract_quote({
        "message": {
            "imageMessage": {
                "caption": "Así la quiero",
                "contextInfo": {
                    "stanzaId": "ORIG-FOTO",
                    "quotedMessage": {"imageMessage": {"caption": "Modelo nuevo"}},
                },
            }
        }
    })

    assert quote_id == "ORIG-FOTO"
    assert "Modelo nuevo" in quote_body


def test_context_at_item_level_is_read_too():
    quote_id, quote_body = extract_quote({
        "message": {"conversation": "dale"},
        "contextInfo": {"stanzaId": "ORIG-2", "quotedMessage": {"conversation": "¿Te lo envío hoy?"}},
    })

    assert (quote_id, quote_body) == ("ORIG-2", "¿Te lo envío hoy?")


def test_plain_message_is_not_a_reply():
    parsed = parse_messages_upsert(_upsert({"conversation": "Hola"}))

    assert parsed[0]["quoted_id"] == ""
    assert parsed[0]["quoted_body"] == ""


def test_ai_knows_what_the_client_is_answering():
    def msg(direction, body, quoted=None):
        return SimpleNamespace(direction=direction, source="agent", body=body, transcript=None, quoted_body=quoted)

    messages = [
        msg("out", "¿Te sirve el martes o el jueves?"),
        msg("in", "Ese", "¿Te sirve el martes o el jueves?"),
    ]
    history = format_history(messages)

    assert history[-1]["role"] == "user"
    assert history[-1]["content"] == "(respondiendo a «¿Te sirve el martes o el jueves?») Ese"


def _register(client) -> dict:
    tag = uuid.uuid4().hex[:8]
    response = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Citas {tag}",
            "owner_name": "Owner",
            "email": f"citas-{tag}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def chat(client):
    from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus
    from app.infrastructure.persistence.database import SessionLocal

    owner = _register(client)
    tenant_id = uuid.UUID(owner["tenant_id"])
    with SessionLocal() as db:
        tenant = db.get(Tenant, tenant_id)
        tenant.whatsapp_status = WhatsAppStatus.CONNECTED.value
        session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()
        if session is None:
            session = WhatsAppSession(tenant_id=tenant_id, instance_name=f"inst_{uuid.uuid4().hex[:8]}")
            db.add(session)
        session.status = WhatsAppStatus.CONNECTED.value
        conversation = Conversation(
            tenant_id=tenant_id,
            contact_phone="+573001234567",
            contact_name="Julián",
            whatsapp_connection_id=uuid.uuid4(),
        )
        db.add(conversation)
        db.flush()
        original = Message(
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            direction="in",
            source="contact",
            body="¿Cuánto vale el corte?",
            status="received",
            evolution_message_id="ORIG-CORTE:3",
        )
        db.add(original)
        db.commit()
        ids = conversation.id, original.id
    return ids, {"Authorization": f"Bearer {owner['access_token']}"}


@requires_db
@patch("app.application.whatsapp.whatsapp_service.gateway_send_text", return_value={"key": {"id": "WA-RESP-1"}})
def test_owner_replies_quoting_the_client_message(mock_send, client, chat):
    (conversation_id, original_id), headers = chat
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"text": "Vale 25 mil", "reply_to_message_id": str(original_id)},
        headers=headers,
    )

    assert response.status_code in (200, 201), response.text
    body = response.json()
    assert body["quoted_evolution_id"] == "ORIG-CORTE"
    assert body["quoted_body"] == "¿Cuánto vale el corte?"
    quoted = mock_send.call_args.kwargs["quoted"]
    assert quoted["key"] == {"id": "ORIG-CORTE", "remoteJid": "573001234567@s.whatsapp.net", "fromMe": False}
    assert quoted["message"] == {"conversation": "¿Cuánto vale el corte?"}


@requires_db
@patch("app.application.whatsapp.whatsapp_service.gateway_send_text", return_value={"key": {"id": "WA-RESP-2"}})
def test_cannot_quote_a_message_from_another_chat(mock_send, client, chat):
    (conversation_id, _), headers = chat
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"text": "Hola", "reply_to_message_id": str(uuid.uuid4())},
        headers=headers,
    )

    assert response.status_code in (200, 201), response.text
    assert response.json()["quoted_evolution_id"] is None
    assert mock_send.call_args.kwargs["quoted"] is None

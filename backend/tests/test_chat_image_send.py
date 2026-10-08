"""El dueño pega o adjunta una foto en el chat del panel y le llega al cliente por WhatsApp."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from tests.conftest import requires_db

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _register(client) -> dict:
    tag = uuid.uuid4().hex[:8]
    response = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Fotos {tag}",
            "owner_name": "Owner",
            "email": f"fotos-{tag}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def chat(client):
    from app.domain.entities import Conversation, Tenant, WhatsAppSession
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
        db.commit()
        conversation_id = conversation.id
    return conversation_id, {"Authorization": f"Bearer {owner['access_token']}"}


def _post(client, conversation_id, headers, *, data: bytes, caption: str = "", name: str = "foto.png"):
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages/image",
        files={"file": (name, data, "image/png")},
        data={"caption": caption},
        headers=headers,
    )


@requires_db
@patch("app.application.whatsapp.whatsapp_service.gateway_send_image", return_value={"key": {"id": "WA-FOTO-1"}})
def test_pasted_photo_reaches_the_client_and_shows_in_the_chat(mock_send, client, chat):
    from app.domain.entities import Message
    from app.infrastructure.persistence.database import SessionLocal

    conversation_id, headers = chat
    response = _post(client, conversation_id, headers, data=PNG, caption="Así quedó el corte 💈")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["body"] == "[Imagen]\nAsí quedó el corte 💈"
    assert body["source"] == "agent"
    kwargs = mock_send.call_args.kwargs
    assert kwargs["mimetype"] == "image/png"
    assert kwargs["caption"] == "Así quedó el corte 💈"
    with SessionLocal() as db:
        row = db.get(Message, uuid.UUID(body["id"]))
        assert row.evolution_message_id == "WA-FOTO-1"
        assert row.direction == "out"


@requires_db
@patch("app.application.whatsapp.whatsapp_service.gateway_send_image", return_value={"key": {"id": "WA-FOTO-2"}})
def test_the_real_file_type_counts_not_its_name(mock_send, client, chat):
    conversation_id, headers = chat

    assert _post(client, conversation_id, headers, data=JPEG, name="captura.png").status_code == 201
    assert mock_send.call_args.kwargs["mimetype"] == "image/jpeg"

    rejected = _post(client, conversation_id, headers, data=b"%PDF-1.7 no soy foto", name="foto.png")
    assert rejected.status_code == 400
    assert "JPG, PNG o WEBP" in rejected.json()["detail"]


@requires_db
@patch("app.application.whatsapp.whatsapp_service.gateway_send_image")
def test_huge_photos_are_rejected_before_reaching_whatsapp(mock_send, client, chat):
    conversation_id, headers = chat
    huge = PNG + b"\x00" * (5 * 1024 * 1024)

    response = _post(client, conversation_id, headers, data=huge)

    assert response.status_code == 413
    mock_send.assert_not_called()


@requires_db
@patch("app.application.whatsapp.whatsapp_service.gateway_send_image")
def test_another_business_cannot_send_photos_to_this_chat(mock_send, client, chat):
    conversation_id, _headers = chat
    intruder = _register(client)

    response = _post(
        client, conversation_id, {"Authorization": f"Bearer {intruder['access_token']}"}, data=PNG
    )

    assert response.status_code == 404
    mock_send.assert_not_called()

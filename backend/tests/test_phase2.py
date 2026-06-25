from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.application.messaging.message_service import parse_messages_upsert
from app.application.messaging.webhook_processor import process_evolution_webhook
from app.application.whatsapp.whatsapp_status import can_send_whatsapp, resolve_whatsapp_status
from app.domain.entities.enums import WhatsAppStatus
from tests.conftest import requires_db


def test_parse_messages_upsert_includes_from_me():
    payload = {
        "messages": [
            {
                "key": {
                    "remoteJid": "573001234567@s.whatsapp.net",
                    "fromMe": False,
                    "id": "ABC123",
                },
                "message": {"conversation": "Hola, me interesa"},
                "pushName": "Juan",
            },
            {
                "key": {
                    "remoteJid": "573001234567@s.whatsapp.net",
                    "fromMe": True,
                    "id": "XYZ",
                },
                "message": {"conversation": "Respuesta desde celular"},
            },
        ]
    }
    parsed = parse_messages_upsert(payload)
    assert len(parsed) == 2
    inbound = next(p for p in parsed if not p["from_me"])
    outbound = next(p for p in parsed if p["from_me"])
    assert inbound["body"] == "Hola, me interesa"
    assert outbound["body"] == "Respuesta desde celular"


def test_resolve_whatsapp_status_banned():
    assert resolve_whatsapp_status("close", {"statusReason": "blocked"}) == WhatsAppStatus.BANNED.value
    assert resolve_whatsapp_status("open", {}) == WhatsAppStatus.CONNECTED.value


def test_can_send_whatsapp_blocks_when_disconnected():
    ok, msg = can_send_whatsapp(WhatsAppStatus.DISCONNECTED.value)
    assert ok is False
    assert "QR" in msg


@requires_db
def test_webhook_processor_connection_update(client: TestClient):
    auth = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": "WA Test",
            "owner_name": "Owner",
            "email": f"wa-{uuid.uuid4().hex[:8]}@test.com",
            "password": "password123",
        },
    )
    if auth.status_code == 503:
        pytest.skip("Plan no disponible en DB de test")
    assert auth.status_code == 201
    tenant_id = auth.json()["tenant_id"]

    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import WhatsAppSession

    with SessionLocal() as db:
        session = WhatsAppSession(
            tenant_id=uuid.UUID(tenant_id),
            instance_name=f"t_{tenant_id.replace('-', '')[:16]}",
            status=WhatsAppStatus.CONNECTING.value,
        )
        db.add(session)
        db.commit()

    process_evolution_webhook(
        uuid.UUID(tenant_id),
        {
            "event": "connection.update",
            "instance": f"t_{tenant_id.replace('-', '')[:16]}",
            "data": {"state": "open", "owner": "573001234567@s.whatsapp.net"},
        },
    )

    with SessionLocal() as db:
        session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == uuid.UUID(tenant_id)).one()
        assert session.status == WhatsAppStatus.CONNECTED.value
        assert session.phone_number == "+573001234567"
        assert session.active_connection_id is not None


@requires_db
def test_webhook_rejects_wrong_instance(client: TestClient):
    auth = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": "WA Wrong Instance",
            "owner_name": "Owner",
            "email": f"wi-{uuid.uuid4().hex[:8]}@test.com",
            "password": "password123",
        },
    )
    if auth.status_code == 503:
        pytest.skip("Plan no disponible")
    tenant_id = uuid.UUID(auth.json()["tenant_id"])
    instance_name = f"t_{uuid.uuid4().hex[:12]}"

    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import WhatsAppSession, Message

    with SessionLocal() as db:
        db.add(
            WhatsAppSession(
                tenant_id=tenant_id,
                instance_name=instance_name,
                status=WhatsAppStatus.CONNECTING.value,
            )
        )
        db.commit()

    process_evolution_webhook(
        tenant_id,
        {
            "event": "messages.upsert",
            "instance": "t_wronginstance",
            "data": {
                "key": {
                    "remoteJid": "573001234567@s.whatsapp.net",
                    "fromMe": False,
                    "id": "MSG999",
                },
                "message": {"conversation": "no debe guardarse"},
            },
        },
    )

    with SessionLocal() as db:
        count = db.query(Message).filter(Message.tenant_id == tenant_id).count()
        assert count == 0


@requires_db
@patch("app.application.whatsapp.whatsapp_service.evolution_client")
def test_connect_whatsapp_returns_qr(mock_evo, client: TestClient):
    mock_evo.instance_exists.return_value = False
    mock_evo.create_instance.return_value = {"instance": {"instanceName": "t_test"}}
    mock_evo.connect_instance.return_value = {"base64": "data:image/png;base64,abc"}

    auth = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": "Connect Test",
            "owner_name": "Owner",
            "email": f"conn-{uuid.uuid4().hex[:8]}@test.com",
            "password": "password123",
        },
    )
    if auth.status_code == 503:
        pytest.skip("Plan no disponible")
    token = auth.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post("/api/v1/whatsapp/connect", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "connecting"
    assert body["qr_base64"] is not None

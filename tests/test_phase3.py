from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.models import Conversation
from app.services.realtime_service import publish_conversation_updated
from tests.conftest import requires_db
from tests.test_phase1 import _register


@requires_db
def test_update_conversation_ai(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    from app.database import SessionLocal

    with SessionLocal() as db:
        conv = Conversation(
            tenant_id=uuid.UUID(auth["tenant_id"]),
            contact_phone="+573001112233",
            contact_name="Test Contact",
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)
        conv_id = conv.id

    response = client.patch(
        f"/api/v1/conversations/{conv_id}/ai",
        headers=headers,
        json={"ai_active": True},
    )
    assert response.status_code == 200
    assert response.json()["ai_active"] is True


@requires_db
def test_mode_update_publishes_event(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    from app.database import SessionLocal

    with SessionLocal() as db:
        conv = Conversation(
            tenant_id=uuid.UUID(auth["tenant_id"]),
            contact_phone="+573004445566",
            contact_name="Mode Test",
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)
        conv_id = conv.id

    with patch("app.api.conversations.publish_conversation_updated") as mock_pub:
        response = client.patch(
            f"/api/v1/conversations/{conv_id}/mode",
            headers=headers,
            json={"mode": "manual"},
        )
        assert response.status_code == 200
        assert response.json()["mode"] == "manual"
        mock_pub.assert_called_once()


@requires_db
def test_panel_websocket_auth(client: TestClient):
    auth = _register(client)
    token = auth["access_token"]

    with pytest.raises(Exception):
        with client.websocket_connect("/ws/panel?token=invalid"):
            pass

    with client.websocket_connect(f"/ws/panel?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "connected"
        assert hello["tenant_id"] == auth["tenant_id"]


@requires_db
def test_panel_websocket_receives_redis_event(client: TestClient):
    auth = _register(client)
    token = auth["access_token"]
    tenant_id = auth["tenant_id"]

    from app.database import SessionLocal

    with SessionLocal() as db:
        conv = Conversation(
            tenant_id=uuid.UUID(tenant_id),
            contact_phone="+573007778899",
            contact_name="WS Test",
            ai_active=True,
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)

    with client.websocket_connect(f"/ws/panel?token={token}") as ws:
        ws.receive_json()
        publish_conversation_updated(tenant_id, conv)
        event = json.loads(ws.receive_text())
        assert event["type"] == "conversation.updated"
        assert event["conversation"]["id"] == str(conv.id)


def test_panel_route(client: TestClient):
    response = client.get("/panel")
    assert response.status_code == 200
    assert "SaasChatbot" in response.text

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import requires_db
from tests.test_phase1 import _register


def _conversation(tenant_id: uuid.UUID) -> uuid.UUID:
    from app.domain.entities import Conversation
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        conversation = Conversation(
            tenant_id=tenant_id,
            contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
            contact_name="Julián",
            whatsapp_connection_id=uuid.uuid4(),
            ai_active=True,
        )
        db.add(conversation)
        db.commit()
        return conversation.id


@requires_db
def test_manual_mode_and_ai_are_mutually_exclusive(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    conversation_id = _conversation(uuid.UUID(auth["tenant_id"]))
    base = f"/api/v1/conversations/{conversation_id}"

    manual = client.patch(f"{base}/mode", json={"mode": "manual"}, headers=headers)
    assert manual.status_code == 200, manual.text
    assert manual.json()["mode"] == "manual"
    assert manual.json()["ai_active"] is False

    ai_on = client.patch(f"{base}/ai", json={"ai_active": True}, headers=headers)
    assert ai_on.status_code == 200, ai_on.text
    assert ai_on.json()["ai_active"] is True
    assert ai_on.json()["mode"] == "auto"

    ai_off = client.patch(f"{base}/ai", json={"ai_active": False}, headers=headers)
    assert ai_off.json()["ai_active"] is False
    assert ai_off.json()["mode"] == "auto"

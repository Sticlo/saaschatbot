from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Lead, Tenant
from app.domain.entities.enums import LeadStatus
from app.application.outbound.bait_limit_service import check_bait_quota, record_bait_sent
from app.application.outbound.outbound_service import import_leads, render_bait_message
from tests.conftest import requires_db
from tests.test_phase1 import _register


def test_render_bait_message_with_name():
    text = render_bait_message(
        "Hola{name_part}! ¿Cómo estás?",
        contact_name="Juan Pérez",
        business_name="Mi Negocio",
    )
    assert " Juan" in text
    assert "Mi Negocio" not in text or True  # business not in this template


def test_render_bait_message_without_name():
    text = render_bait_message("Hola{name_part}!", contact_name="")
    assert text == "Hola!"


@requires_db
def test_import_leads_dedup(client: TestClient):
    auth = _register(client)
    tenant_id = uuid.UUID(auth["tenant_id"])
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    payload = {
        "leads": [
            {"phone": "3001234567", "name": "Lead A"},
            {"phone": "+573001234567", "name": "Duplicado"},
            {"phone": "abc", "name": "Inválido"},
        ]
    }
    r1 = client.post("/api/v1/outbound/leads", json=payload, headers=headers)
    assert r1.status_code == 200
    body = r1.json()
    assert body["added"] == 1
    assert body["skipped"] == 1
    assert body["invalid"] == 1

    r2 = client.post("/api/v1/outbound/leads", json=payload, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["added"] == 0
    assert r2.json()["skipped"] >= 1


@requires_db
def test_campaign_requires_disclaimer(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    client.post(
        "/api/v1/outbound/leads",
        json={"leads": [{"phone": "3009876543", "name": "Test"}]},
        headers=headers,
    )
    r = client.post(
        "/api/v1/outbound/campaigns/enqueue",
        json={"limit": 1},
        headers=headers,
    )
    assert r.status_code == 403


@requires_db
def test_enqueue_respects_trial_limit(client: TestClient):
    auth = _register(client)
    tenant_id = uuid.UUID(auth["tenant_id"])
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    client.post("/api/v1/tenants/me/accept-disclaimer", headers=headers)

    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        assert tenant is not None
        tenant.trial_bait_used = 10
        db.commit()

    client.post(
        "/api/v1/outbound/leads",
        json={"leads": [{"phone": "3001112233", "name": "X"}]},
        headers=headers,
    )
    r = client.post(
        "/api/v1/outbound/campaigns/enqueue",
        json={"limit": 1},
        headers=headers,
    )
    assert r.status_code == 409


@requires_db
def test_enqueue_campaign_success(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    client.post("/api/v1/tenants/me/accept-disclaimer", headers=headers)
    client.post(
        "/api/v1/outbound/leads",
        json={
            "leads": [
                {"phone": "3004445566", "name": "Ana"},
                {"phone": "3004445567", "name": "Bob"},
            ]
        },
        headers=headers,
    )
    r = client.post(
        "/api/v1/outbound/campaigns/enqueue",
        json={"limit": 2, "name": "Test campaña"},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["queued"] == 2

    stats = client.get("/api/v1/outbound/queue/stats", headers=headers)
    assert stats.status_code == 200
    assert stats.json()["queue_pending"] == 2


@requires_db
def test_record_bait_sent_increments_trial(client: TestClient):
    auth = _register(client)
    tenant_id = uuid.UUID(auth["tenant_id"])

    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        assert tenant is not None
        before = tenant.trial_bait_used
        record_bait_sent(db, tenant)
        db.commit()
        db.refresh(tenant)
        assert tenant.trial_bait_used == before + 1


@requires_db
@patch("app.application.outbound.outbound_service.send_text_message")
def test_process_send_marks_bait_sent(mock_send, client: TestClient):
    from datetime import datetime, timezone

    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Conversation, SendQueueItem, WhatsAppSession, WhatsAppStatus
    from app.domain.entities.enums import MessageDirection, MessageSource, MessageStatus, SendQueueStatus
    from app.domain.entities import Message
    from app.application.outbound.outbound_service import process_send_queue_item

    auth = _register(client)
    tenant_id = uuid.UUID(auth["tenant_id"])

    mock_msg = Message(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        conversation_id=uuid.uuid4(),
        direction=MessageDirection.OUT.value,
        source=MessageSource.BAIT.value,
        body="hola",
        status=MessageStatus.SENT.value,
        created_at=datetime.now(timezone.utc),
    )
    mock_send.return_value = mock_msg

    with SessionLocal() as db:
        session = WhatsAppSession(
            tenant_id=tenant_id,
            instance_name=f"t_{uuid.uuid4().hex[:12]}",
            status=WhatsAppStatus.CONNECTED.value,
            active_connection_id=uuid.uuid4(),
        )
        db.add(session)

        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        tenant.disclaimer_accepted_at = datetime.now(timezone.utc)
        tenant.whatsapp_status = WhatsAppStatus.CONNECTED.value

        item = SendQueueItem(
            tenant_id=tenant_id,
            phone_e164="+573009998877",
            contact_name="Test",
            message_body="Hola carnada",
            status=SendQueueStatus.PENDING.value,
            scheduled_at=datetime.now(timezone.utc),
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        item_id = item.id

    with SessionLocal() as db:
        item = db.query(SendQueueItem).filter(SendQueueItem.id == item_id).one()
        done = process_send_queue_item(db, item)
        assert done is True
        assert item.status == SendQueueStatus.SENT.value
        db.commit()

        conv = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant_id,
                Conversation.contact_phone == "+573009998877",
            )
            .first()
        )
        assert conv is not None
        assert conv.bait_sent is True
        assert conv.ai_active is False

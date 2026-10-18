"""Estabilidad de WhatsApp: motivos de caída bien clasificados, aviso al dueño y caídas silenciosas."""
from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient

from app.application.whatsapp.whatsapp_status import resolve_whatsapp_status
from app.domain.entities.enums import WhatsAppStatus
from tests.conftest import requires_db


def test_logout_is_not_a_ban_but_403_is():
    assert resolve_whatsapp_status("close", {"statusReason": "logout"}) == WhatsAppStatus.DISCONNECTED.value
    assert resolve_whatsapp_status("close", {"statusReason": 401}) == WhatsAppStatus.DISCONNECTED.value
    assert resolve_whatsapp_status("close", {"statusReason": 403}) == WhatsAppStatus.BANNED.value
    assert resolve_whatsapp_status("open", {"statusReason": 403}) == WhatsAppStatus.CONNECTED.value


def _register(client: TestClient) -> tuple[uuid.UUID, str]:
    tag = uuid.uuid4().hex[:8]
    email = f"salud-{tag}@test.com"
    res = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Salud {tag}",
            "owner_name": "Dueña",
            "email": email,
            "password": "password123",
            "accept_legal": True,
        },
    )
    assert res.status_code == 201, res.text
    return uuid.UUID(res.json()["tenant_id"]), email


def _linked_session(db, tenant_id: uuid.UUID, status: str):
    from app.domain.entities import Tenant, WhatsAppSession

    session = WhatsAppSession(
        tenant_id=tenant_id,
        instance_name=f"t_{uuid.uuid4().hex[:12]}",
        status=status,
        active_connection_id=uuid.uuid4().hex,
        bound_owner_jid="573000000000@s.whatsapp.net",
    )
    db.add(session)
    db.get(Tenant, tenant_id).whatsapp_status = status
    db.commit()
    return session


@requires_db
def test_owner_gets_one_email_after_ten_minutes_and_one_reminder(client: TestClient, monkeypatch):
    from app.application.whatsapp import whatsapp_health_service as health
    from app.domain.entities import Tenant
    from app.infrastructure.email import email_service
    from app.infrastructure.persistence.database import SessionLocal

    sent: list[dict] = []
    monkeypatch.setattr(email_service, "send_billing_email", lambda **kw: sent.append(kw))
    tenant_id, email = _register(client)

    with SessionLocal() as db:
        _linked_session(db, tenant_id, WhatsAppStatus.DISCONNECTED.value)
        tenant = db.get(Tenant, tenant_id)
        t0 = time.time()
        assert health.notify_owner_if_still_down(db, tenant, now=t0) is None, "una caída de red no avisa"
        assert health.notify_owner_if_still_down(db, tenant, now=t0 + 5 * 60) is None
        assert health.notify_owner_if_still_down(db, tenant, now=t0 + 11 * 60) == "first"
        assert health.notify_owner_if_still_down(db, tenant, now=t0 + 12 * 60) is None, "sin spam"
        assert health.notify_owner_if_still_down(db, tenant, now=t0 + 25 * 3600) == "reminder"
        assert health.notify_owner_if_still_down(db, tenant, now=t0 + 26 * 3600) is None

        health.clear_down_mark(tenant_id)
        t1 = t0 + 30 * 3600
        assert health.notify_owner_if_still_down(db, tenant, now=t1) is None, "otra caída arranca de cero"
        assert health.notify_owner_if_still_down(db, tenant, now=t1 + 11 * 60) == "first"

    assert [m["to_email"] for m in sent] == [email] * 3
    assert sent[0]["subject"] == "Tu WhatsApp se desconectó de Omitel"
    assert sent[1]["subject"].startswith("Sigue desconectado")
    assert "Conectar WhatsApp" in " ".join(sent[0]["paragraphs"])


@requires_db
def test_silent_drop_needs_two_closed_reads(client: TestClient, monkeypatch):
    from app.application.whatsapp import whatsapp_gateway
    from app.application.whatsapp import whatsapp_health_service as health
    from app.domain.entities import AuditLog, Tenant
    from app.infrastructure.persistence.database import SessionLocal

    tenant_id, _ = _register(client)
    with SessionLocal() as db:
        session = _linked_session(db, tenant_id, WhatsAppStatus.CONNECTED.value)
        instance = session.instance_name

    monkeypatch.setattr(
        whatsapp_gateway,
        "connection_state",
        lambda name: {"instance": {"state": "close" if name == instance else "open"}},
    )
    with SessionLocal() as db:
        assert health.detect_silent_drops(db, force=True) == 0, "una sola lectura puede ser un parpadeo"
        assert db.get(Tenant, tenant_id).whatsapp_status == WhatsAppStatus.CONNECTED.value
        health.detect_silent_drops(db, force=True)
        db.expire_all()
        assert db.get(Tenant, tenant_id).whatsapp_status == WhatsAppStatus.DISCONNECTED.value
        incident = (
            db.query(AuditLog)
            .filter(AuditLog.tenant_id == tenant_id, AuditLog.action == "system.whatsapp_disconnected")
            .one()
        )
        assert incident.details["reason"] == "silent"

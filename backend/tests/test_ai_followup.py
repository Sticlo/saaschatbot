from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tests.conftest import requires_db
from tests.test_phase1 import _register

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # 10:00 a. m. en Colombia


def _setup(db, *, followup_hours: int = 4):
    from app.domain.entities import Tenant, TenantProfile, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus

    tenant = Tenant(
        business_name="Barbería Norte",
        slug=f"barberia-{uuid.uuid4().hex[:8]}",
        whatsapp_status=WhatsAppStatus.CONNECTED.value,
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
    tenant.profile = TenantProfile(followup_hours=followup_hours)
    db.flush()
    db.refresh(tenant)
    return tenant


def _chat(db, tenant, name, *, history, silent_hours, interest=None, mode="auto", followup_sent_at=None):
    """history: fuentes en orden; el último mensaje se envió hace `silent_hours`."""
    from app.domain.entities import Conversation, Message
    from app.domain.entities.enums import MessageDirection

    last_at = NOW - timedelta(hours=silent_hours)
    conversation = Conversation(
        tenant_id=tenant.id,
        contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
        contact_name=name,
        ai_active=mode == "auto",
        mode=mode,
        interest_status=interest,
        last_message_at=last_at,
        followup_sent_at=followup_sent_at,
    )
    db.add(conversation)
    db.flush()
    for i, source in enumerate(history):
        db.add(
            Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                direction=MessageDirection.IN.value if source == "contact" else MessageDirection.OUT.value,
                source=source,
                body="¿Cuánto vale el corte con barba?" if source == "contact" else "El corte con barba vale $35.000",
                status="received",
                created_at=last_at - timedelta(minutes=len(history) - 1 - i),
            )
        )
    db.flush()
    return conversation


@requires_db
def test_only_chats_where_the_client_went_quiet_after_the_ai_are_due():
    from app.application.ai.ai_followup_service import due_followups
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _setup(db)
        _chat(db, tenant, "Calló hace 5 h", history=("contact", "bot"), silent_hours=5)
        _chat(db, tenant, "Nuevo silencio tras seguimiento", history=("contact", "bot"), silent_hours=6,
              followup_sent_at=NOW - timedelta(hours=30))
        _chat(db, tenant, "Apenas 2 h", history=("contact", "bot"), silent_hours=2)
        _chat(db, tenant, "Chat viejo", history=("contact", "bot"), silent_hours=30)
        _chat(db, tenant, "Cliente escribió de último", history=("bot", "contact"), silent_hours=5)
        _chat(db, tenant, "El dueño escribió", history=("contact", "bot", "agent"), silent_hours=5)
        _chat(db, tenant, "Nunca escribió", history=("bait",), silent_hours=5)
        _chat(db, tenant, "No le interesa", history=("contact", "bot"), silent_hours=5, interest="not_interested")
        _chat(db, tenant, "Modo manual", history=("contact", "bot"), silent_hours=5, mode="manual")
        _chat(db, tenant, "Ya se le escribió", history=("contact", "bot"), silent_hours=5,
              followup_sent_at=NOW - timedelta(hours=1))

        names = {c.contact_name for c in due_followups(db, tenant, NOW)}
        assert names == {"Calló hace 5 h", "Nuevo silencio tras seguimiento"}
        db.rollback()


@requires_db
def test_followup_is_sent_once_and_skipped_when_the_chat_already_ended(monkeypatch):
    from app.application.ai import ai_followup_service as svc
    from app.application.whatsapp import whatsapp_service
    from app.infrastructure.cache.redis_client import get_redis
    from app.infrastructure.persistence.database import SessionLocal

    sent: list[str] = []
    monkeypatch.setattr(whatsapp_service, "send_text_message", lambda db, **kw: sent.append(kw["text"]))

    with SessionLocal() as db:
        tenant = _setup(db)
        quiet = _chat(db, tenant, "Julián", history=("contact", "bot"), silent_hours=5)
        ended = _chat(db, tenant, "Marta", history=("contact", "bot"), silent_hours=5)
        try:
            monkeypatch.setattr(svc, "chat_completion", lambda *_a, **_k: "«¿Sigues interesado en el corte con barba?»")
            assert svc.send_followup(db, tenant, quiet, NOW) is True
            assert sent == ["¿Sigues interesado en el corte con barba?"]
            assert quiet.followup_sent_at == NOW
            assert {c.contact_name for c in svc.due_followups(db, tenant, NOW)} == {"Marta"}

            monkeypatch.setattr(svc, "chat_completion", lambda *_a, **_k: "NO")
            assert svc.send_followup(db, tenant, ended, NOW) is False
            assert len(sent) == 1
            assert svc.due_followups(db, tenant, NOW) == []
        finally:
            get_redis().delete(f"ai:followup:{quiet.id}", f"ai:followup:{ended.id}")


def test_never_writes_at_night_or_outside_business_hours():
    from types import SimpleNamespace

    from app.application.ai.ai_followup_service import within_sending_hours
    from app.application.appointments.appointment_service import BOGOTA

    profile = SimpleNamespace(schedule_open_time="06:00", schedule_close_time="18:00")
    at = lambda hour: datetime(2026, 10, 14, hour, 0, tzinfo=BOGOTA)
    assert within_sending_hours(profile, at(7)) is False
    assert within_sending_hours(profile, at(9)) is True
    assert within_sending_hours(profile, at(17)) is True
    assert within_sending_hours(profile, at(19)) is False
    late = SimpleNamespace(schedule_open_time="10:00", schedule_close_time="23:00")
    assert within_sending_hours(profile=late, now=at(9)) is False
    assert within_sending_hours(profile=late, now=at(19)) is True
    assert within_sending_hours(profile=late, now=at(21)) is False


@requires_db
def test_each_business_chooses_how_long_to_wait(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    assert client.get("/api/v1/tenants/me/followup", headers=headers).json() == {"followup_hours": 4}
    r = client.put("/api/v1/tenants/me/followup", json={"followup_hours": 12}, headers=headers)
    assert r.status_code == 200, r.text
    assert client.get("/api/v1/tenants/me/followup", headers=headers).json() == {"followup_hours": 12}
    assert client.put("/api/v1/tenants/me/followup", json={"followup_hours": 0}, headers=headers).json() == {
        "followup_hours": 0
    }
    assert client.put("/api/v1/tenants/me/followup", json={"followup_hours": 3}, headers=headers).status_code == 422

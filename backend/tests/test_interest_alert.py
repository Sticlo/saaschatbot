from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.conftest import requires_db
from tests.test_phase1 import _register


OWNER = {"name": "Dueña", "phone": "+573009998877", "scope": "all"}
DISPATCH = {"name": "Carlos", "phone": "+573005556644", "scope": "sales"}


def _setup(db, *, threshold: int, recipients: list[dict] | None = None):
    from app.domain.entities import Tenant, TenantProfile, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus

    tenant = Tenant(
        business_name="Motel Luna",
        slug=f"motel-luna-{uuid.uuid4().hex[:8]}",
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
    tenant.profile = TenantProfile(
        alert_recipients=recipients if recipients is not None else [OWNER],
        alert_threshold=threshold,
    )
    db.flush()
    db.refresh(tenant)
    return tenant


def _conversation(db, tenant, *, name: str, interest="interested", history=()):
    """history: fuentes en orden cronológico, p. ej. ("contact", "bot", "agent")."""
    from app.domain.entities import Conversation, Message
    from app.domain.entities.enums import MessageDirection

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
        contact_name=name,
        interest_status=interest,
    )
    db.add(conversation)
    db.flush()
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    for i, source in enumerate(history):
        db.add(
            Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                direction=MessageDirection.IN.value if source == "contact" else MessageDirection.OUT.value,
                source=source,
                body=f"{source} {i}",
                status="received",
                created_at=start + timedelta(minutes=i),
            )
        )
    db.flush()
    return conversation


@requires_db
def test_only_interested_without_human_reply_count():
    from app.application.conversations.interest_alert_service import unanswered_interested_conversations
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _setup(db, threshold=20)
        _conversation(db, tenant, name="Sin respuesta", history=("contact",))
        _conversation(db, tenant, name="Solo bot", history=("contact", "bot"))
        _conversation(db, tenant, name="Volvió a escribir", history=("contact", "agent", "contact"))
        _conversation(db, tenant, name="Atendido", history=("contact", "agent"))
        _conversation(db, tenant, name="No interesado", interest="not_interested", history=("contact",))
        _conversation(db, tenant, name="Sin etiqueta", interest=None, history=("contact",))

        names = {c.contact_name for c in unanswered_interested_conversations(db, tenant.id)}
        assert names == {"Sin respuesta", "Solo bot", "Volvió a escribir"}
        db.rollback()


@requires_db
def test_alert_fires_once_per_threshold_crossing(monkeypatch):
    from app.application.conversations import interest_alert_service as svc
    from app.infrastructure.cache.redis_client import get_redis
    from app.infrastructure.persistence.database import SessionLocal

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(svc, "send_alert", lambda _session, phone, text: sent.append((phone, text)))

    with SessionLocal() as db:
        tenant = _setup(db, threshold=2)
        key = svc._alert_key(tenant.id)
        try:
            _conversation(db, tenant, name="Ana", history=("contact",))
            assert svc.check_interest_backlog(db, tenant) is False

            pedro = _conversation(db, tenant, name="Pedro", history=("contact",))
            assert svc.check_interest_backlog(db, tenant) is True
            assert len(sent) == 1
            phone, text = sent[0]
            assert phone == "+573009998877"
            assert "2 clientes interesados" in text and "Ana" in text and "Pedro" in text

            assert svc.check_interest_backlog(db, tenant) is False
            assert len(sent) == 1

            pedro.interest_status = "not_interested"
            db.flush()
            assert svc.check_interest_backlog(db, tenant) is False

            pedro.interest_status = "interested"
            db.flush()
            assert svc.check_interest_backlog(db, tenant) is True
            assert len(sent) == 2
        finally:
            get_redis().delete(key)
            db.rollback()


@requires_db
def test_failed_send_retries_on_next_check(monkeypatch):
    from app.application.conversations import interest_alert_service as svc
    from app.infrastructure.cache.redis_client import get_redis
    from app.infrastructure.persistence.database import SessionLocal

    def boom(*_a, **_kw):
        raise RuntimeError("gateway caído")

    monkeypatch.setattr(svc, "send_alert", boom)
    with SessionLocal() as db:
        tenant = _setup(db, threshold=1)
        key = svc._alert_key(tenant.id)
        try:
            _conversation(db, tenant, name="Ana", history=("contact",))
            assert svc.check_interest_backlog(db, tenant) is False
            assert not get_redis().exists(key)
        finally:
            get_redis().delete(key)
            db.rollback()


@requires_db
def test_backlog_alert_goes_to_everyone_on_all_alerts_but_not_to_dispatch(monkeypatch):
    from app.application.conversations import interest_alert_service as svc
    from app.infrastructure.cache.redis_client import get_redis
    from app.infrastructure.persistence.database import SessionLocal

    sent: list[str] = []
    monkeypatch.setattr(svc, "send_alert", lambda _session, phone, _text: sent.append(phone))
    manager = {"name": "Encargado", "phone": "+573001231234", "scope": "all"}

    with SessionLocal() as db:
        tenant = _setup(db, threshold=1, recipients=[OWNER, DISPATCH, manager])
        key = svc._alert_key(tenant.id)
        try:
            _conversation(db, tenant, name="Ana", history=("contact",))
            assert svc.check_interest_backlog(db, tenant) is True
            assert sorted(sent) == sorted([OWNER["phone"], manager["phone"]])
        finally:
            get_redis().delete(key)
            db.rollback()


@requires_db
def test_one_broken_number_does_not_block_the_rest(monkeypatch):
    from app.application.conversations import interest_alert_service as svc
    from app.infrastructure.cache.redis_client import get_redis
    from app.infrastructure.persistence.database import SessionLocal

    delivered: list[str] = []

    def flaky(_session, phone, _text):
        if phone == OWNER["phone"]:
            raise RuntimeError("número sin WhatsApp")
        delivered.append(phone)

    monkeypatch.setattr(svc, "send_alert", flaky)
    manager = {"name": "Encargado", "phone": "+573001231234", "scope": "all"}
    with SessionLocal() as db:
        tenant = _setup(db, threshold=1, recipients=[OWNER, manager])
        key = svc._alert_key(tenant.id)
        try:
            _conversation(db, tenant, name="Ana", history=("contact",))
            assert svc.check_interest_backlog(db, tenant) is True
            assert delivered == [manager["phone"]]
        finally:
            get_redis().delete(key)
            db.rollback()


def test_alert_phone_validation():
    from app.application.conversations.interest_alert_service import InterestAlertError, normalize_alert_phone

    assert normalize_alert_phone("+57 300 999 8877") == "+573009998877"
    assert normalize_alert_phone("") == ""
    with pytest.raises(InterestAlertError):
        normalize_alert_phone("123")


def test_recipients_are_cleaned_deduplicated_and_capped():
    from app.application.conversations.interest_alert_service import (
        InterestAlertError,
        normalize_alert_recipients,
    )

    cleaned = normalize_alert_recipients(
        [
            {"name": "  Dueña  ", "phone": "+57 300 999 8877", "scope": "all"},
            {"name": "Fila vacía", "phone": "", "scope": "sales"},
            {"name": "Repetido", "phone": "+57 3009998877", "scope": "sales"},
            {"name": "Carlos", "phone": "+57 300 555 6644", "scope": "sales"},
            {"name": "Sin tipo", "phone": "+57 300 123 1234", "scope": "raro"},
        ]
    )
    assert cleaned == [
        {"name": "Dueña", "phone": "+573009998877", "scope": "all"},
        {"name": "Carlos", "phone": "+573005556644", "scope": "sales"},
        {"name": "Sin tipo", "phone": "+573001231234", "scope": "all"},
    ]

    with pytest.raises(InterestAlertError, match="Pedro"):
        normalize_alert_recipients([{"name": "Pedro", "phone": "123", "scope": "sales"}])
    too_many = [{"name": "", "phone": f"+57300000000{i}", "scope": "sales"} for i in range(6)]
    with pytest.raises(InterestAlertError):
        normalize_alert_recipients(too_many)


def test_team_numbers_never_get_ai():
    from app.application.conversations.interest_alert_service import is_alert_phone
    from app.domain.entities import Tenant, TenantProfile

    tenant = Tenant(business_name="X", slug="x")
    tenant.profile = TenantProfile(alert_recipients=[OWNER, DISPATCH])
    assert is_alert_phone(tenant, "+573005556644") is True
    assert is_alert_phone(tenant, "+573009998877") is True
    assert is_alert_phone(tenant, "+573001112233") is False


@requires_db
def test_interest_alert_settings_api(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    r = client.get("/api/v1/tenants/me/interest-alert", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"recipients": [], "alert_threshold": 10, "pending_count": 0}

    r = client.put(
        "/api/v1/tenants/me/interest-alert",
        json={
            "recipients": [
                {"name": "Yo", "phone": "+57 300 999 8877", "scope": "all"},
                {"name": "Carlos", "phone": "+57 300 555 6644", "scope": "sales"},
            ],
            "alert_threshold": 15,
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["recipients"] == [
        {"name": "Yo", "phone": "+573009998877", "scope": "all"},
        {"name": "Carlos", "phone": "+573005556644", "scope": "sales"},
    ]
    assert r.json()["alert_threshold"] == 15

    r = client.put(
        "/api/v1/tenants/me/interest-alert",
        json={"recipients": [{"name": "Mal", "phone": "123", "scope": "all"}], "alert_threshold": 15},
        headers=headers,
    )
    assert r.status_code == 422

    r = client.post("/api/v1/tenants/me/interest-alert/test", headers=headers)
    assert r.status_code == 400
    assert "WhatsApp" in r.json()["detail"]

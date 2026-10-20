"""Integración con siochat: origen del registro, consulta de estado y avisos firmados."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.application.auth import magic_link_service
from app.application.auth.oauth_service import OAuthProfile
from app.application.integration import siochat
from app.config import settings
from app.domain.entities import IntegrationEvent, Subscription, Tenant, User, WhatsAppSession
from tests.conftest import requires_db

pytestmark = requires_db

TOKEN = "token-de-prueba-siochat"
SECRET = "secreto-de-prueba-siochat"


def _phone() -> str:
    return "5730" + str(uuid.uuid4().int)[:8]


@pytest.fixture()
def integration_on(monkeypatch):
    monkeypatch.setattr(settings, "integration_api_token", TOKEN)
    monkeypatch.setattr(settings, "integration_webhook_url", "https://siochat.test/api/omitel/eventos")
    monkeypatch.setattr(settings, "omitel_webhook_secret", SECRET)


@pytest.fixture()
def sent(monkeypatch):
    """Lo que habría llegado a siochat; `status` decide la respuesta."""
    calls: list[dict] = []
    state = {"status": 200}

    def fake_post(url, *, content, headers, timeout):
        calls.append({"url": url, "body": content, "headers": headers})
        return httpx.Response(state["status"])

    monkeypatch.setattr(siochat.httpx, "post", fake_post)
    return calls, state


def _tenant_by_email(email: str) -> Tenant:
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        tenant = db.get(Tenant, user.tenant_id)
        db.expunge(tenant)
        return tenant


def _events(tenant_id, event: str | None = None) -> list[IntegrationEvent]:
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        query = db.query(IntegrationEvent).filter(IntegrationEvent.tenant_id == tenant_id)
        if event:
            query = query.filter(IntegrationEvent.event == event)
        rows = query.order_by(IntegrationEvent.created_at).all()
        for row in rows:
            db.expunge(row)
        return rows


def _only_pending_for(tenant_id) -> None:
    """Eventos de otras pruebas pendientes llenarían el lote de envío."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        db.query(IntegrationEvent).filter(
            IntegrationEvent.tenant_id != tenant_id, IntegrationEvent.delivered_at.is_(None)
        ).update({IntegrationEvent.delivered_at: datetime.now(timezone.utc)}, synchronize_session=False)
        db.commit()


def _register(client: TestClient, attribution: dict | None = None) -> tuple[str, Tenant]:
    email = f"sio-{uuid.uuid4().hex[:8]}@test.com"
    body = {
        "business_name": "Negocio Sio",
        "owner_name": "Dueño Sio",
        "email": email,
        "password": "password123",
        "accept_legal": True,
    }
    if attribution is not None:
        body["attribution"] = attribution
    res = client.post("/api/v1/auth/register", json=body)
    assert res.status_code in (200, 201), res.text
    return email, _tenant_by_email(email)


def _estado(client: TestClient, phone: str, token: str = TOKEN):
    return client.get(
        "/api/integracion/estado", params={"telefono": phone}, headers={"Authorization": f"Bearer {token}"}
    )


def test_normalize_phone_and_clean_attribution():
    assert siochat.normalize_phone("300 123 4567") == "573001234567"
    assert siochat.normalize_phone("+57 300-123-4567") == "573001234567"
    assert siochat.normalize_phone("12345") is None
    clean = siochat.clean_attribution(
        {"utm_source": " whatsapp ", "utm_medium": "", "lead": "3001234567", "otro": "x", "ref": "a" * 300}
    )
    assert clean == {"utm_source": "whatsapp", "lead": "573001234567", "ref": "a" * 100}


def test_password_signup_keeps_attribution_and_status_endpoint(client: TestClient, integration_on):
    lead = _phone()
    _, tenant = _register(
        client,
        {"utm_source": "whatsapp", "utm_medium": "prospeccion", "utm_campaign": "omitel_asistente_ia", "lead": lead},
    )
    assert tenant.lead_phone == lead
    assert tenant.signup_attribution["utm_campaign"] == "omitel_asistente_ia"

    data = _estado(client, lead).json()
    assert data["registrado"] is True
    assert data["estado"] == "prueba"
    assert data["lead"] == lead
    assert data["utm_source"] == "whatsapp"
    assert data["whatsapp_conectado"] is False
    assert data["pagos"] == {"cantidad": 0, "total_cop": 0, "ultimo": None}

    [registro] = _events(tenant.id, "registro")
    assert registro.payload["lead"] == lead
    assert registro.payload["estado"] == "prueba"
    assert registro.payload["id"] == str(registro.id)


def test_status_endpoint_auth_and_unknown_phone(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "integration_api_token", "")
    assert _estado(client, "573001234567").status_code == 404

    monkeypatch.setattr(settings, "integration_api_token", TOKEN)
    assert _estado(client, "573001234567", token="otro").status_code == 401
    assert _estado(client, "123").status_code == 422
    unknown = _phone()
    assert _estado(client, unknown).json() == {"registrado": False, "telefono": unknown}


def test_status_matches_whatsapp_owner_after_disconnect(client: TestClient, integration_on):
    from app.infrastructure.persistence.database import SessionLocal

    _, tenant = _register(client)
    owner = _phone()
    with SessionLocal() as db:
        db.add(
            WhatsAppSession(
                tenant_id=tenant.id,
                instance_name=f"sio-{uuid.uuid4().hex[:8]}",
                phone_number=None,
                bound_owner_jid=f"{owner}:12@s.whatsapp.net",
            )
        )
        db.commit()
    data = _estado(client, owner).json()
    assert data["registrado"] is True
    assert data["negocio"] == "Negocio Sio"
    assert data["telefono_whatsapp"] == owner


def test_magic_link_signup_keeps_attribution(client: TestClient, monkeypatch, integration_on):
    urls: list[str] = []
    monkeypatch.setattr(
        magic_link_service, "send_magic_link_email", lambda *, to_email, url, signup: urls.append(url)
    )
    email = f"sio-ml-{uuid.uuid4().hex[:8]}@test.com"
    lead = _phone()
    res = client.post(
        "/api/v1/auth/magic-link",
        json={
            "email": email,
            "business_name": "Negocio ML",
            "owner_name": "Dueño ML",
            "accept_legal": True,
            "attribution": {"utm_source": "whatsapp", "lead": lead},
        },
    )
    assert res.json()["sent"] is True
    token = parse_qs(urlparse(urls[-1]).query)["token"][0]
    assert client.post("/api/v1/auth/magic-link/verify", json={"token": token}).status_code == 200

    tenant = _tenant_by_email(email)
    assert tenant.lead_phone == lead
    assert tenant.signup_attribution == {"utm_source": "whatsapp", "lead": lead}


def test_google_signup_keeps_attribution_from_start_url(client: TestClient, monkeypatch, integration_on):
    from app.presentation.api import auth as auth_api

    email = f"sio-g-{uuid.uuid4().hex[:8]}@test.com"
    lead = _phone()
    monkeypatch.setattr(auth_api, "oauth_provider_enabled", lambda provider: True)
    monkeypatch.setattr(
        auth_api,
        "fetch_google_profile",
        lambda code: OAuthProfile(provider="google", subject=uuid.uuid4().hex, email=email, full_name="Dueña G"),
    )
    start = client.get(
        "/api/v1/auth/google/start",
        params={"next": "/panel", "utm_source": "whatsapp", "utm_campaign": "c1", "lead": lead},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = client.get("/api/v1/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert callback.status_code in (302, 303, 307), callback.text

    tenant = _tenant_by_email(email)
    assert tenant.lead_phone == lead
    assert tenant.signup_attribution == {"utm_source": "whatsapp", "utm_campaign": "c1", "lead": lead}


def test_no_events_when_integration_is_off(client: TestClient):
    _, tenant = _register(client, {"utm_source": "x"})
    assert _events(tenant.id) == []


def test_delivery_is_signed_and_retried(client: TestClient, integration_on, sent):
    from app.infrastructure.persistence.database import SessionLocal

    calls, state = sent
    _, tenant = _register(client)
    _only_pending_for(tenant.id)
    [event] = _events(tenant.id, "registro")
    now = datetime.now(timezone.utc) + timedelta(seconds=5)

    state["status"] = 503
    with SessionLocal() as db:
        siochat.deliver_pending_events(db, now=now)
    [failed] = _events(tenant.id, "registro")
    assert failed.attempts == 1
    assert failed.delivered_at is None
    assert failed.last_error == "HTTP 503"
    assert abs((failed.next_attempt_at - now).total_seconds() - 60) < 1

    sent_before = len(calls)
    with SessionLocal() as db:
        siochat.deliver_pending_events(db, now=now + timedelta(seconds=30))
    assert len(calls) == sent_before

    state["status"] = 200
    with SessionLocal() as db:
        siochat.deliver_pending_events(db, now=now + timedelta(minutes=2))
    [delivered] = _events(tenant.id, "registro")
    assert delivered.delivered_at is not None

    call = calls[-1]
    body, headers = call["body"], call["headers"]
    assert json.loads(body)["id"] == str(event.id)
    expected = hmac.new(SECRET.encode(), headers["X-Omitel-Timestamp"].encode() + b"." + body, hashlib.sha256)
    assert headers["X-Omitel-Signature"] == f"sha256={expected.hexdigest()}"
    assert abs(int(headers["X-Omitel-Timestamp"]) - time.time()) < 60


def test_delivery_gives_up_after_all_retries(client: TestClient, integration_on, sent):
    from app.infrastructure.persistence.database import SessionLocal

    calls, state = sent
    state["status"] = 500
    _, tenant = _register(client)
    _only_pending_for(tenant.id)
    moment = datetime.now(timezone.utc) + timedelta(seconds=5)
    for _ in range(len(siochat.RETRY_DELAYS) + 3):
        with SessionLocal() as db:
            siochat.deliver_pending_events(db, now=moment)
        moment += timedelta(hours=3)
    [event] = _events(tenant.id, "registro")
    assert event.attempts == len(siochat.RETRY_DELAYS) + 1
    assert event.delivered_at is None


def test_whatsapp_and_trial_events(client: TestClient, integration_on, monkeypatch):
    from app.application.whatsapp import whatsapp_health_service as health
    from app.infrastructure.persistence.database import SessionLocal

    _, tenant = _register(client)
    health.clear_down_mark(tenant.id)

    def set_status(value: str) -> None:
        with SessionLocal() as db:
            db.get(Tenant, tenant.id).whatsapp_status = value
            db.commit()

    def sweep(now=None) -> None:
        with SessionLocal() as db:
            siochat.sweep_state_events(db, now=now)

    set_status("connected")
    sweep()
    sweep()
    assert len(_events(tenant.id, "whatsapp_conectado")) == 1

    set_status("connecting")
    health.note_outage_start(tenant.id, {"reason": "unknown"})
    sweep()
    assert _events(tenant.id, "whatsapp_desconectado") == []

    sweep(now=datetime.now(timezone.utc) + timedelta(seconds=61))
    assert len(_events(tenant.id, "whatsapp_desconectado")) == 1
    health.clear_down_mark(tenant.id)

    with SessionLocal() as db:
        sub = db.query(Subscription).filter(Subscription.tenant_id == tenant.id).one()
        sub.trial_ends_at = datetime.now(timezone.utc) + timedelta(hours=10)
        db.commit()
    sweep()
    sweep()
    assert len(_events(tenant.id, "prueba_por_vencer")) == 1


def test_short_blip_is_not_an_outage(monkeypatch):
    from app.application.platform import incidents
    from app.application.whatsapp import whatsapp_health_service as health

    recorded: list[str] = []
    monkeypatch.setattr(incidents, "record_incident", lambda tenant_id, action, *a, **k: recorded.append(action))
    tenant_id = uuid.uuid4()
    start = time.time()

    health.note_outage_start(tenant_id, {"reason": "unknown", "code": 200})
    assert health.report_outage_if_lasting(tenant_id, now=start + 5) is False
    health.clear_down_mark(tenant_id)
    assert health.report_outage_if_lasting(tenant_id, now=start + 120) is False
    assert recorded == []

    health.note_outage_start(tenant_id, {"reason": "unknown"})
    assert health.report_outage_if_lasting(tenant_id, now=time.time() + 61) is True
    assert health.report_outage_if_lasting(tenant_id, now=time.time() + 120) is False
    assert recorded == ["system.whatsapp_disconnected"]
    health.clear_down_mark(tenant_id)

"""Consola de plataforma: solo el superadmin ve todas las empresas y les ajusta plan, límites y funciones."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.application.platform.tenant_overrides import (
    feature_allowed,
    limit_override,
    normalize_overrides,
)
from app.config import settings
from tests.conftest import requires_db


def _register(client: TestClient, label: str) -> dict:
    tag = uuid.uuid4().hex[:8]
    email = f"{label}-{tag}@test.com"
    res = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"{label.title()} {tag}",
            "owner_name": "Dueña",
            "email": email,
            "password": "password123",
            "accept_legal": True,
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()
    return {
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
        "tenant_id": data["tenant_id"],
        "email": email,
        "name": f"{label.title()} {tag}",
    }


@pytest.fixture()
def admin_and_client(client: TestClient, monkeypatch):
    admin = _register(client, "plataforma")
    customer = _register(client, "cliente")
    monkeypatch.setattr(settings, "platform_admin_emails", f"otro@x.com, {admin['email'].upper()}")
    return admin, customer


def test_normalize_overrides_keeps_only_known_keys():
    clean = normalize_overrides(
        {
            "features": {"ai_booking": False, "hack": True, "catalog_files": None},
            "limits": {"ai_daily_replies": "25", "max_team_members": None, "otro": 3},
            "note": "  le dimos 15 días  ",
        }
    )
    assert clean == {
        "features": {"ai_booking": False},
        "limits": {"ai_daily_replies": 25},
        "note": "le dimos 15 días",
    }
    assert normalize_overrides({"features": {}, "limits": {}, "note": ""}) == {}
    with pytest.raises(ValueError):
        normalize_overrides({"limits": {"ai_daily_replies": "mucho"}})


def test_feature_and_limit_defaults_follow_the_plan():
    tenant = SimpleNamespace(platform_overrides=None)
    assert feature_allowed(tenant, "ai_booking") is True
    assert limit_override(tenant, "ai_daily_replies") is None
    tenant.platform_overrides = {"features": {"ai_booking": False}, "limits": {"ai_daily_replies": 0}}
    assert feature_allowed(tenant, "ai_booking") is False
    assert limit_override(tenant, "ai_daily_replies") == 0


def test_paused_ai_blocks_replies():
    from app.application.ai.ai_service import ai_block_reason

    conv = SimpleNamespace(ai_active=True, mode="auto", status="open")
    tenant = SimpleNamespace(is_active=True, ai_global_enabled=True, platform_overrides=None)
    assert ai_block_reason(tenant, conv) is None
    tenant.platform_overrides = {"features": {"ai_replies": False}}
    assert "soporte" in ai_block_reason(tenant, conv)
    tenant.is_active = False
    assert ai_block_reason(tenant, conv) == "Empresa suspendida"


@requires_db
def test_console_is_hidden_from_regular_customers(client: TestClient, admin_and_client):
    admin, customer = admin_and_client
    for path in ("/me", "/tenants", f"/tenants/{customer['tenant_id']}", "/plans"):
        assert client.get(f"/api/v1/platform{path}", headers=customer["headers"]).status_code == 404
    res = client.patch(
        f"/api/v1/platform/tenants/{admin['tenant_id']}", headers=customer["headers"], json={"is_active": False}
    )
    assert res.status_code == 404
    assert client.get("/api/v1/platform/me", headers=admin["headers"]).json()["email"] == admin["email"]


@requires_db
def test_admin_lists_and_opens_any_company(client: TestClient, admin_and_client):
    admin, customer = admin_and_client
    rows = client.get(
        "/api/v1/platform/tenants", headers=admin["headers"], params={"q": customer["email"]}
    ).json()
    assert [r["id"] for r in rows] == [customer["tenant_id"]]
    row = rows[0]
    assert row["owner_email"] == customer["email"]
    assert row["subscription"]["status"] == "trial"
    assert row["problems_7d"] == 0

    detail = client.get(f"/api/v1/platform/tenants/{customer['tenant_id']}", headers=admin["headers"]).json()
    assert detail["business_name"] == customer["name"]
    assert [u["email"] for u in detail["users"]] == [customer["email"]]
    assert detail["features"] == {"ai_replies": True, "ai_booking": True, "catalog_files": True}

    history = client.get(
        f"/api/v1/platform/tenants/{customer['tenant_id']}/history", headers=admin["headers"]
    ).json()
    assert any(h["action"] == "tenant.registered" for h in history)


@requires_db
def test_overrides_change_what_the_company_can_do(client: TestClient, admin_and_client):
    admin, customer = admin_and_client
    res = client.patch(
        f"/api/v1/platform/tenants/{customer['tenant_id']}",
        headers=admin["headers"],
        json={
            "overrides": {
                "features": {"ai_booking": False, "catalog_files": False},
                "limits": {"ai_daily_replies": 7, "max_team_members": 1},
                "note": "Cliente piloto",
            }
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["changed"] == ["platform.overrides_changed"]
    assert body["tenant"]["limits"]["ai_daily_replies"] == 7
    assert body["tenant"]["limits"]["max_team_members"] == 1

    schedule = client.get("/api/v1/appointments/schedule", headers=customer["headers"]).json()
    assert schedule["ai_booking_allowed"] is False
    blocked = client.put(
        "/api/v1/appointments/schedule",
        headers=customer["headers"],
        json={"open_time": "08:00", "close_time": "18:00", "slot_minutes": 60, "ai_booking_enabled": True},
    )
    assert blocked.status_code == 403

    upload = client.post(
        "/api/v1/quick-shortcuts/files",
        headers=customer["headers"],
        files={"file": ("menu.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    assert upload.status_code == 403

    invite = client.post(
        "/api/v1/users",
        headers=customer["headers"],
        json={
            "email": f"vendedor-{uuid.uuid4().hex[:6]}@test.com",
            "full_name": "Vendedor",
            "password": "password123",
            "role": "agent",
        },
    )
    assert invite.status_code == 403
    assert "1 en plan" in invite.json()["detail"]

    history = client.get(
        f"/api/v1/platform/tenants/{customer['tenant_id']}/history",
        headers=admin["headers"],
        params={"kind": "platform"},
    ).json()
    assert history[0]["action"] == "platform.overrides_changed"
    assert admin["email"] in history[0]["message"]
    assert history[0]["details"]["after"]["note"] == "Cliente piloto"

    # Vaciar los ajustes devuelve todo a lo que dice el plan.
    reset = client.patch(
        f"/api/v1/platform/tenants/{customer['tenant_id']}",
        headers=admin["headers"],
        json={"overrides": {}},
    ).json()
    assert reset["tenant"]["overrides"] == {}
    assert client.get("/api/v1/appointments/schedule", headers=customer["headers"]).json()["ai_booking_allowed"]


@requires_db
def test_extend_trial_then_grant_paid_days(client: TestClient, admin_and_client):
    admin, customer = admin_and_client
    url = f"/api/v1/platform/tenants/{customer['tenant_id']}"
    now = datetime.now(timezone.utc)

    res = client.patch(url, headers=admin["headers"], json={"extend_trial_days": 5})
    assert res.status_code == 200, res.text
    assert res.json()["changed"] == ["platform.trial_extended"]
    sub = res.json()["tenant"]["subscription"]
    assert sub["status"] == "trial" and sub["trial_days_left"] == settings.trial_days + 5
    trial_end = datetime.fromisoformat(sub["trial_ends_at"])
    assert abs(trial_end - (now + timedelta(days=settings.trial_days + 5))) < timedelta(minutes=5)

    res = client.patch(url, headers=admin["headers"], json={"grant_paid_days": 10})
    assert res.status_code == 200, res.text
    sub = res.json()["tenant"]["subscription"]
    assert sub["status"] == "active" and sub["is_paid"] is True
    end = datetime.fromisoformat(sub["current_period_end"])
    assert abs(end - (now + timedelta(days=10))) < timedelta(minutes=5)

    paid = client.patch(url, headers=admin["headers"], json={"extend_trial_days": 3})
    assert paid.status_code == 400 and "Regalar días" in paid.json()["detail"]


@requires_db
def test_unlimited_plan_never_expires_nor_charges(client: TestClient, admin_and_client):
    from app.application.billing.subscription_service import expire_lapsed_subscriptions
    from app.infrastructure.persistence.database import SessionLocal

    admin, customer = admin_and_client
    res = client.patch(
        f"/api/v1/platform/tenants/{customer['tenant_id']}",
        headers=admin["headers"],
        json={"grant_unlimited": True},
    )
    assert res.status_code == 200, res.text
    sub = res.json()["tenant"]["subscription"]
    assert sub["status"] == "active" and sub["is_paid"] is True
    assert sub["current_period_end"] is None and sub["auto_renew"] is False

    with SessionLocal() as db:
        expire_lapsed_subscriptions(db, now=datetime.now(timezone.utc) + timedelta(days=3650))
    detail = client.get(f"/api/v1/platform/tenants/{customer['tenant_id']}", headers=admin["headers"]).json()
    assert detail["subscription"]["is_paid"] is True
    assert detail["limits"]["ai_daily_replies"] == 0


@requires_db
def test_suspend_cuts_access_but_not_your_own_company(client: TestClient, admin_and_client):
    admin, customer = admin_and_client
    own = client.patch(
        f"/api/v1/platform/tenants/{admin['tenant_id']}", headers=admin["headers"], json={"is_active": False}
    )
    assert own.status_code == 400

    res = client.patch(
        f"/api/v1/platform/tenants/{customer['tenant_id']}", headers=admin["headers"], json={"is_active": False}
    )
    assert res.status_code == 200, res.text
    assert client.get("/api/v1/auth/me", headers=customer["headers"]).status_code == 401

    back = client.patch(
        f"/api/v1/platform/tenants/{customer['tenant_id']}", headers=admin["headers"], json={"is_active": True}
    )
    assert back.json()["changed"] == ["platform.reactivated"]
    assert client.get("/api/v1/auth/me", headers=customer["headers"]).status_code == 200


@requires_db
def test_incidents_are_throttled_and_counted_as_problems(client: TestClient, admin_and_client):
    from app.application.platform.incidents import record_incident

    admin, customer = admin_and_client
    assert record_incident(customer["tenant_id"], "system.ai_send_failed", "Falló el envío") is True
    assert record_incident(customer["tenant_id"], "system.ai_send_failed", "Falló otra vez") is False

    rows = client.get(
        "/api/v1/platform/tenants", headers=admin["headers"], params={"q": customer["email"]}
    ).json()
    assert rows[0]["problems_7d"] == 1

    problems = client.get(
        f"/api/v1/platform/tenants/{customer['tenant_id']}/history",
        headers=admin["headers"],
        params={"kind": "problems"},
    ).json()
    assert [p["message"] for p in problems] == ["Falló el envío"]
    assert problems[0]["is_problem"] is True


@requires_db
def test_accounts_show_login_method_and_activation(client: TestClient, admin_and_client):
    from app.domain.entities import WhatsAppSession
    from app.infrastructure.persistence.database import SessionLocal

    admin, customer = admin_and_client
    [row] = client.get(
        "/api/v1/platform/accounts", headers=admin["headers"], params={"q": customer["email"]}
    ).json()
    assert row["login_method"] == "password"
    assert row["activation"] == "pending"
    assert row["last_login_at"] is not None  # registrarse ya es entrar
    assert row["tenant_id"] == customer["tenant_id"]

    with SessionLocal() as db:
        db.add(
            WhatsAppSession(
                tenant_id=uuid.UUID(customer["tenant_id"]),
                instance_name=f"t_{uuid.uuid4().hex[:12]}",
                last_connected_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    [tenant_row] = client.get(
        "/api/v1/platform/tenants", headers=admin["headers"], params={"q": customer["email"]}
    ).json()
    assert tenant_row["activation"] == "active"


def test_admin_page_is_served_with_csp(client: TestClient):
    res = client.get("/panel/admin")
    assert res.status_code == 200
    assert "Consola de plataforma" in res.text
    assert "script-src 'self'" in res.headers["content-security-policy"]
    assert "noindex" in res.headers.get("x-robots-tag", "")

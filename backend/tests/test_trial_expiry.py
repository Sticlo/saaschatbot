"""Prueba gratis de 3 días: al vencer la IA se detiene, se avisa una vez y pagar o extender la reactiva."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import settings
from app.domain.entities.enums import SubscriptionStatus
from tests.conftest import requires_db


def _trial(ends_at, status=SubscriptionStatus.TRIAL.value, period_end=None):
    return SimpleNamespace(status=status, trial_ends_at=ends_at, current_period_end=period_end)


def test_trial_window_and_days_left():
    from app.application.billing.subscription_service import (
        service_lapsed,
        trial_days_left,
        trial_has_expired,
        trial_is_running,
    )

    now = datetime.now(timezone.utc)
    running = _trial(now + timedelta(days=2, hours=3))
    assert trial_is_running(running, now) and not trial_has_expired(running, now)
    assert trial_days_left(running, now) == 3
    assert trial_days_left(_trial(now + timedelta(hours=2)), now) == 1

    over = _trial(now - timedelta(minutes=1))
    assert not trial_is_running(over, now) and trial_has_expired(over, now)
    assert trial_days_left(over, now) is None
    assert service_lapsed(over)

    swept = _trial(now - timedelta(days=1), status=SubscriptionStatus.PAST_DUE.value)
    assert trial_has_expired(swept, now)
    paid_later = _trial(now - timedelta(days=40), status=SubscriptionStatus.ACTIVE.value, period_end=now + timedelta(days=5))
    assert not trial_has_expired(paid_later, now) and not service_lapsed(paid_later)
    assert not service_lapsed(None), "sin suscripción no se corta nada"


def _register(client: TestClient) -> dict:
    tag = uuid.uuid4().hex[:8]
    email = f"prueba-{tag}@test.com"
    res = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Prueba {tag}",
            "owner_name": "Dueña",
            "email": email,
            "password": "password123",
            "accept_legal": True,
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()
    return {"headers": {"Authorization": f"Bearer {data['access_token']}"}, "tenant_id": data["tenant_id"], "email": email}


def _expire_trial(tenant_id: str) -> None:
    from app.domain.entities import Subscription
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        db.query(Subscription).filter(Subscription.tenant_id == uuid.UUID(tenant_id)).update(
            {Subscription.trial_ends_at: datetime.now(timezone.utc) - timedelta(minutes=5)}
        )
        db.commit()


def _quotas(tenant_id: str):
    from app.application.ai.ai_usage_service import check_daily_classify_quota, check_daily_reply_quota
    from app.domain.entities import Tenant
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = db.get(Tenant, uuid.UUID(tenant_id))
        return check_daily_reply_quota(db, tenant), check_daily_classify_quota(db, tenant)


@requires_db
def test_new_account_gets_three_day_trial(client: TestClient):
    account = _register(client)
    sub = client.get("/api/v1/subscriptions/me", headers=account["headers"]).json()
    assert sub["is_trial"] is True and sub["trial_expired"] is False
    assert sub["trial_days_left"] == settings.trial_days == 3
    end = datetime.fromisoformat(sub["trial_ends_at"])
    assert abs(end - (datetime.now(timezone.utc) + timedelta(days=3))) < timedelta(minutes=5)
    (reply_ok, *_), (classify_ok, *_) = _quotas(account["tenant_id"])
    assert reply_ok and classify_ok


@requires_db
def test_expired_trial_stops_ai_and_is_announced_once(client: TestClient, monkeypatch):
    from app.application.billing import auto_renew_service
    from app.domain.entities import AuditLog, Tenant
    from app.domain.entities.enums import TenantPlan
    from app.infrastructure.persistence.database import SessionLocal

    sent: list[dict] = []
    monkeypatch.setattr(auto_renew_service, "send_billing_email", lambda **kw: sent.append(kw))

    account = _register(client)
    _expire_trial(account["tenant_id"])

    sub = client.get("/api/v1/subscriptions/me", headers=account["headers"]).json()
    assert sub["is_trial"] is False and sub["trial_expired"] is True
    assert sub["needs_payment"] is True and sub["status"] == "past_due", "el panel lo ve antes del barrido"
    (reply_ok, _, _, reply_err), (classify_ok, *_) = _quotas(account["tenant_id"])
    assert not reply_ok and not classify_ok
    assert "prueba gratis terminó" in reply_err
    assert client.get("/api/v1/ai/status", headers=account["headers"]).json()["plan_required"] is True

    with SessionLocal() as db:
        auto_renew_service.process_auto_renewals(db)
        auto_renew_service.process_auto_renewals(db)
        tenant = db.get(Tenant, uuid.UUID(account["tenant_id"]))
        assert tenant.plan == TenantPlan.SUSPENDED.value
        logs = db.query(AuditLog).filter(AuditLog.tenant_id == tenant.id, AuditLog.action == "billing.trial_expired").count()
        assert logs == 1
    mine = [mail for mail in sent if mail["to_email"] == account["email"]]
    assert len(mine) == 1 and "prueba" in mine[0]["subject"].lower()

    sub = client.get("/api/v1/subscriptions/me", headers=account["headers"]).json()
    assert sub["status"] == "past_due" and sub["trial_expired"] is True
    cancel = client.post(
        "/api/v1/billing/subscription/cancel",
        headers=account["headers"],
        json={"reason": "otro", "feedback": ""},
    )
    assert cancel.status_code == 400, cancel.text
    assert "prueba gratis" in cancel.json()["detail"]


@requires_db
def test_admin_can_extend_an_expired_trial(client: TestClient, monkeypatch):
    from app.application.billing import auto_renew_service
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr(auto_renew_service, "send_billing_email", lambda **kw: None)
    admin = _register(client)
    customer = _register(client)
    monkeypatch.setattr(settings, "platform_admin_emails", admin["email"])
    _expire_trial(customer["tenant_id"])
    with SessionLocal() as db:
        auto_renew_service.process_auto_renewals(db)

    res = client.patch(
        f"/api/v1/platform/tenants/{customer['tenant_id']}",
        headers=admin["headers"],
        json={"extend_trial_days": 2},
    )
    assert res.status_code == 200, res.text
    sub = res.json()["tenant"]["subscription"]
    assert sub["status"] == "trial" and sub["trial_expired"] is False and sub["trial_days_left"] == 2
    (reply_ok, *_), _ = _quotas(customer["tenant_id"])
    assert reply_ok

"""Cobro automático (fuentes de pago Wompi) y cancelación de suscripciones."""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.application.billing import auto_renew_service
from app.application.billing.auto_renew_service import MAX_RENEWAL_ATTEMPTS, process_auto_renewals
from app.application.billing.checkout_service import handle_wompi_event
from app.domain.entities import AuditLog, Subscription, SubscriptionStatus, Tenant, TenantPlan
from app.domain.entities.payment_checkout import PaymentCheckout, PaymentCheckoutKind
from app.infrastructure.persistence.database import SessionLocal
from tests.conftest import requires_db

pytestmark = requires_db


class FakeWompi:
    def __init__(self):
        self.transaction_status = "APPROVED"
        self.status_message = None
        self.charge_delay = 0.0
        self.charges: list[dict] = []
        self.voided: list[str] = []
        self.lookup: dict[str, dict] = {}
        self._next_source = 9000

    def create_payment_source(self, *, source_type, token, customer_email):
        self._next_source += 1
        public = {"type": source_type}
        if source_type == "NEQUI":
            public["phone_number"] = "3001234567"
        return {"id": self._next_source, "status": "AVAILABLE", "type": source_type, "public_data": public}

    def create_source_transaction(self, **kwargs):
        if self.charge_delay:
            time.sleep(self.charge_delay)
        self.charges.append(kwargs)
        tx = {"id": f"tx-{len(self.charges)}", "status": self.transaction_status, "reference": kwargs["reference"]}
        if self.status_message:
            tx["status_message"] = self.status_message
        return tx

    def void_payment_source(self, source_id):
        self.voided.append(source_id)

    def fetch_transaction(self, transaction_id):
        return self.lookup.get(transaction_id)

    def find_transaction_by_reference(self, reference):
        return None


@pytest.fixture()
def wompi(monkeypatch):
    fake = FakeWompi()
    for name in (
        "create_payment_source",
        "create_source_transaction",
        "void_payment_source",
        "fetch_transaction",
        "find_transaction_by_reference",
    ):
        monkeypatch.setattr(auto_renew_service, name, getattr(fake, name))
    monkeypatch.setattr(auto_renew_service, "wompi_sync_enabled", lambda: True)
    return fake


def _disable_test_auto_renewals() -> None:
    """Los barridos son globales: que ningún negocio de estas pruebas quede cobrable."""
    with SessionLocal() as db:
        tenant_ids = [t.id for t in db.query(Tenant.id).filter(Tenant.business_name.like("Cobro %")).all()]
        if tenant_ids:
            db.query(Subscription).filter(Subscription.tenant_id.in_(tenant_ids)).update(
                {Subscription.auto_renew: False, Subscription.cancel_at_period_end: False},
                synchronize_session=False,
            )
            db.commit()


@pytest.fixture(autouse=True)
def _isolate_sweeps():
    _disable_test_auto_renewals()
    yield
    _disable_test_auto_renewals()


def _charges_for(fake: FakeWompi, tenant_id: str) -> list[dict]:
    prefix = f"om-{tenant_id.replace('-', '')[:8]}-"
    return [charge for charge in fake.charges if charge["reference"].startswith(prefix)]


def _mails_for(sent: list[dict], email: str) -> list[dict]:
    return [mail for mail in sent if mail["to_email"] == email]


@pytest.fixture()
def emails(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(auto_renew_service, "send_billing_email", lambda **kw: sent.append(kw))
    return sent


def _register(client: TestClient) -> tuple[dict, str, str]:
    tag = uuid.uuid4().hex[:8]
    res = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Cobro {tag}",
            "owner_name": "Dueña",
            "email": f"cobro-{tag}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    assert res.status_code == 201, res.text
    token = res.json()["access_token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    return {"Authorization": f"Bearer {token}"}, me["tenant_id"], me["email"]


def _card(**extra) -> dict:
    return {
        "type": "CARD",
        "token": f"tok_test_{uuid.uuid4().hex}",
        "accept_auto_renew": True,
        "accept_wompi_terms": True,
        "brand": "VISA",
        "last_four": "4242",
        **extra,
    }


def _subscription(tenant_id: str) -> Subscription:
    with SessionLocal() as db:
        sub = db.query(Subscription).filter(Subscription.tenant_id == uuid.UUID(tenant_id)).one()
        db.expunge(sub)
        return sub


def _checkouts(tenant_id: str, kind: str | None = None) -> list[PaymentCheckout]:
    with SessionLocal() as db:
        query = db.query(PaymentCheckout).filter(PaymentCheckout.tenant_id == uuid.UUID(tenant_id))
        if kind:
            query = query.filter(PaymentCheckout.kind == kind)
        rows = query.order_by(PaymentCheckout.created_at.asc()).all()
        for row in rows:
            db.expunge(row)
        return rows


def _make_paid_with_card(tenant_id: str, *, ends_in: timedelta) -> datetime:
    """Plan pagado con tarjeta guardada y cobro automático, que vence en `ends_in`."""
    now = datetime.now(timezone.utc)
    end = now + ends_in
    with SessionLocal() as db:
        sub = db.query(Subscription).filter(Subscription.tenant_id == uuid.UUID(tenant_id)).one()
        tenant = db.get(Tenant, sub.tenant_id)
        sub.status = SubscriptionStatus.ACTIVE.value
        sub.current_period_start = end - timedelta(days=30)
        sub.current_period_end = end
        sub.auto_renew = True
        sub.payment_source_id = "777"
        sub.payment_method_type = "CARD"
        sub.payment_method_label = "VISA •••• 4242"
        tenant.plan = TenantPlan.PAID.value
        db.commit()
    return end


def _run_sweep(now: datetime | None = None) -> dict:
    with SessionLocal() as db:
        return process_auto_renewals(db, now=now)


# —— Autorización y primer cobro ——


def test_payment_method_requires_explicit_authorization(client: TestClient, wompi):
    headers, tenant_id, email = _register(client)
    res = client.post("/api/v1/billing/payment-method", json=_card(accept_auto_renew=False), headers=headers)
    assert res.status_code == 400
    res = client.post("/api/v1/billing/payment-method", json=_card(accept_wompi_terms=False), headers=headers)
    assert res.status_code == 400
    assert _subscription(tenant_id).auto_renew is False
    assert _charges_for(wompi, tenant_id) == []


def test_payment_method_requires_login(client: TestClient):
    assert client.post("/api/v1/billing/payment-method", json=_card()).status_code == 401
    assert client.post("/api/v1/billing/subscription/cancel", json={"reason": "precio"}).status_code == 401


def test_trial_card_charges_first_month_and_records_authorization(client: TestClient, wompi, emails):
    headers, tenant_id, email = _register(client)
    res = client.post("/api/v1/billing/payment-method", json=_card(plan_slug="pro"), headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["auto_renew"] is True
    assert body["payment_method_label"] == "VISA •••• 4242"
    assert body["charge"]["status"] == "approved"

    sub = _subscription(tenant_id)
    assert sub.status == SubscriptionStatus.ACTIVE.value
    assert sub.auto_renew_accepted_at is not None
    days = (sub.current_period_end - datetime.now(timezone.utc)).days
    assert 29 <= days <= 30

    assert len(_charges_for(wompi, tenant_id)) == 1
    assert _charges_for(wompi, tenant_id)[0]["amount_in_cents"] == 200_000 * 100
    assert _charges_for(wompi, tenant_id)[0]["source_type"] == "CARD"

    with SessionLocal() as db:
        audit = (
            db.query(AuditLog)
            .filter(AuditLog.tenant_id == uuid.UUID(tenant_id), AuditLog.action == "billing.auto_renew_authorized")
            .one()
        )
        assert audit.details["amount_cop"] == 200_000
        assert audit.details["payment_method"] == "VISA •••• 4242"
        assert audit.ip_address
    assert any("Recibimos tu pago" in mail["subject"] for mail in _mails_for(emails, email))

    summary = client.get("/api/v1/subscriptions/me", headers=headers).json()
    assert summary["is_paid"] is True
    assert summary["auto_renew"] is True
    assert summary["payment_method_label"] == "VISA •••• 4242"
    assert summary["next_charge_at"] is not None


def test_declined_first_charge_returns_reason_and_keeps_trial(client: TestClient, wompi, emails):
    wompi.transaction_status = "DECLINED"
    wompi.status_message = "Fondos insuficientes"
    headers, tenant_id, email = _register(client)
    res = client.post("/api/v1/billing/payment-method", json=_card(plan_slug="pro"), headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["charge"]["status"] == "declined"
    assert res.json()["charge_error"] == "Fondos insuficientes"
    assert _subscription(tenant_id).status == SubscriptionStatus.TRIAL.value


def test_nequi_label_and_replacing_method_voids_previous(client: TestClient, wompi, emails):
    headers, tenant_id, email = _register(client)
    assert client.post("/api/v1/billing/payment-method", json=_card(plan_slug="pro"), headers=headers).status_code == 200
    first_source = _subscription(tenant_id).payment_source_id

    nequi = {
        "type": "NEQUI",
        "token": f"nequi_test_{uuid.uuid4().hex}",
        "accept_auto_renew": True,
        "accept_wompi_terms": True,
    }
    res = client.post("/api/v1/billing/payment-method", json=nequi, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["payment_method_label"] == "Nequi •••• 4567"
    assert res.json()["charge"] is None  # ya pagó este periodo: el cambio no cobra
    assert wompi.voided == [first_source]
    assert len(_charges_for(wompi, tenant_id)) == 1


# —— Renovación automática ——


def test_renewal_charges_once_and_extends_from_period_end(client: TestClient, wompi, emails):
    _, tenant_id, email = _register(client)
    old_end = _make_paid_with_card(tenant_id, ends_in=timedelta(hours=12))

    _run_sweep()
    _run_sweep()

    renewals = _checkouts(tenant_id, PaymentCheckoutKind.RENEWAL)
    assert len(renewals) == 1
    assert renewals[0].status == "approved"
    assert len(_charges_for(wompi, tenant_id)) == 1
    sub = _subscription(tenant_id)
    assert sub.current_period_end == old_end + timedelta(days=30)
    assert sub.renewal_attempts == 0


def test_concurrent_sweeps_charge_only_once(client: TestClient, wompi, emails):
    _, tenant_id, email = _register(client)
    _make_paid_with_card(tenant_id, ends_in=timedelta(hours=6))
    wompi.transaction_status = "PENDING"
    wompi.charge_delay = 0.3

    threads = [threading.Thread(target=_run_sweep) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(_checkouts(tenant_id, PaymentCheckoutKind.RENEWAL)) == 1
    assert len(_charges_for(wompi, tenant_id)) == 1


def test_not_due_yet_is_not_charged(client: TestClient, wompi, emails):
    _, tenant_id, email = _register(client)
    _make_paid_with_card(tenant_id, ends_in=timedelta(days=10))
    _run_sweep()
    assert _charges_for(wompi, tenant_id) == []


def test_declined_renewal_retries_daily_then_stops(client: TestClient, wompi, emails):
    wompi.transaction_status = "DECLINED"
    wompi.status_message = "Tarjeta vencida"
    _, tenant_id, email = _register(client)
    _make_paid_with_card(tenant_id, ends_in=timedelta(hours=12))

    start = datetime.now(timezone.utc)
    _run_sweep(start)
    _run_sweep(start + timedelta(hours=2))  # aún no toca reintentar
    assert len(_charges_for(wompi, tenant_id)) == 1
    _run_sweep(start + timedelta(days=1, minutes=5))
    _run_sweep(start + timedelta(days=2, minutes=10))
    _run_sweep(start + timedelta(days=3, minutes=15))

    assert len(_charges_for(wompi, tenant_id)) == MAX_RENEWAL_ATTEMPTS
    sub = _subscription(tenant_id)
    assert sub.renewal_attempts == MAX_RENEWAL_ATTEMPTS
    assert sub.next_renewal_attempt_at is None
    failure_mails = [mail for mail in _mails_for(emails, email) if "No pudimos cobrar" in mail["subject"]]
    assert len(failure_mails) == 2  # primer rechazo y último intento


def test_pending_renewal_is_reconciled_without_double_charge(client: TestClient, wompi, emails):
    wompi.transaction_status = "PENDING"
    _, tenant_id, email = _register(client)
    old_end = _make_paid_with_card(tenant_id, ends_in=timedelta(hours=12))

    _run_sweep()
    _run_sweep()  # sigue pendiente: no se cobra otra vez
    assert len(_charges_for(wompi, tenant_id)) == 1

    [pending] = _checkouts(tenant_id, PaymentCheckoutKind.RENEWAL)
    wompi.lookup[pending.wompi_transaction_id] = {
        "id": pending.wompi_transaction_id,
        "status": "APPROVED",
        "reference": pending.reference,
        "amount_in_cents": pending.amount_in_cents,
        "currency": pending.currency or "COP",
    }
    _run_sweep(datetime.now(timezone.utc) + timedelta(minutes=5))

    [renewal] = _checkouts(tenant_id, PaymentCheckoutKind.RENEWAL)
    assert renewal.status == "approved"
    assert _subscription(tenant_id).current_period_end == old_end + timedelta(days=30)


def test_webhook_decline_counts_as_failed_attempt(client: TestClient, wompi, emails):
    wompi.transaction_status = "PENDING"
    _, tenant_id, email = _register(client)
    _make_paid_with_card(tenant_id, ends_in=timedelta(hours=12))
    _run_sweep()
    [renewal] = _checkouts(tenant_id, PaymentCheckoutKind.RENEWAL)

    event = {
        "event": "transaction.updated",
        "data": {
            "transaction": {
                "id": renewal.wompi_transaction_id,
                "reference": renewal.reference,
                "status": "DECLINED",
                "amount_in_cents": renewal.amount_in_cents,
                "currency": renewal.currency or "COP",
            }
        },
    }
    with SessionLocal() as db:
        handle_wompi_event(db, event)
        db.commit()
        handle_wompi_event(db, event)  # Wompi reenvía eventos: no debe contar dos veces
        db.commit()

    assert _subscription(tenant_id).renewal_attempts == 1


def test_reminder_is_sent_once_per_period(client: TestClient, wompi, emails):
    _, tenant_id, email = _register(client)
    _make_paid_with_card(tenant_id, ends_in=timedelta(days=2, hours=12))
    _run_sweep()
    _run_sweep()
    reminders = [mail for mail in _mails_for(emails, email) if "Pronto renovamos" in mail["subject"]]
    assert len(reminders) == 1
    assert "$200.000" in reminders[0]["paragraphs"][0]
    assert "VISA •••• 4242" in reminders[0]["paragraphs"][0]
    assert _charges_for(wompi, tenant_id) == []


# —— Cancelación ——


def test_cancel_keeps_access_until_period_end_then_ends(client: TestClient, wompi, emails):
    headers, tenant_id, email = _register(client)
    end = _make_paid_with_card(tenant_id, ends_in=timedelta(days=12))

    res = client.post(
        "/api/v1/billing/subscription/cancel",
        json={"reason": "precio", "feedback": "Muy caro para mi tienda"},
        headers=headers,
    )
    assert res.status_code == 204, res.text

    summary = client.get("/api/v1/subscriptions/me", headers=headers).json()
    assert summary["is_paid"] is True
    assert summary["cancel_at_period_end"] is True
    assert summary["auto_renew"] is False
    assert summary["next_charge_at"] is None
    assert any("Cancelaste" in mail["subject"] for mail in _mails_for(emails, email))

    _run_sweep(end - timedelta(hours=6))  # dentro de la ventana de cobro: no se cobra
    assert _charges_for(wompi, tenant_id) == []

    _run_sweep(end + timedelta(minutes=1))
    sub = _subscription(tenant_id)
    assert sub.status == SubscriptionStatus.CANCELLED.value
    assert sub.cancel_reason == "precio"
    with SessionLocal() as db:
        assert db.get(Tenant, uuid.UUID(tenant_id)).plan == TenantPlan.SUSPENDED.value
    assert _charges_for(wompi, tenant_id) == []


def test_resume_before_period_end_restores_auto_renew(client: TestClient, wompi, emails):
    headers, tenant_id, email = _register(client)
    _make_paid_with_card(tenant_id, ends_in=timedelta(days=12))
    assert client.post("/api/v1/billing/subscription/cancel", json={"reason": "otro"}, headers=headers).status_code == 204
    assert client.post("/api/v1/billing/subscription/resume", headers=headers).status_code == 204

    sub = _subscription(tenant_id)
    assert sub.cancel_at_period_end is False
    assert sub.auto_renew is True
    assert sub.cancel_reason is None


def test_cancel_twice_and_trial_cancel_are_rejected(client: TestClient, wompi, emails):
    headers, tenant_id, email = _register(client)
    trial = client.post("/api/v1/billing/subscription/cancel", json={"reason": "precio"}, headers=headers)
    assert trial.status_code == 400

    _make_paid_with_card(tenant_id, ends_in=timedelta(days=5))
    assert client.post("/api/v1/billing/subscription/cancel", json={"reason": "precio"}, headers=headers).status_code == 204
    again = client.post("/api/v1/billing/subscription/cancel", json={"reason": "precio"}, headers=headers)
    assert again.status_code == 400


def test_lapsed_subscription_cancels_immediately(client: TestClient, wompi, emails):
    headers, tenant_id, email = _register(client)
    _make_paid_with_card(tenant_id, ends_in=timedelta(days=-10))
    assert client.post("/api/v1/billing/subscription/cancel", json={"reason": "resultados"}, headers=headers).status_code == 204
    sub = _subscription(tenant_id)
    assert sub.status == SubscriptionStatus.CANCELLED.value
    assert sub.auto_renew is False


def test_remove_payment_method_stops_auto_renew(client: TestClient, wompi, emails):
    headers, tenant_id, email = _register(client)
    _make_paid_with_card(tenant_id, ends_in=timedelta(hours=12))
    res = client.delete("/api/v1/billing/payment-method", headers=headers)
    assert res.status_code == 204, res.text
    assert wompi.voided == ["777"]

    sub = _subscription(tenant_id)
    assert sub.auto_renew is False
    assert sub.payment_source_id is None
    assert sub.status == SubscriptionStatus.ACTIVE.value  # el plan pagado sigue

    _run_sweep()
    assert _charges_for(wompi, tenant_id) == []

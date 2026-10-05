"""Wompi billing helpers."""

from __future__ import annotations

from app.application.billing.wompi_service import (
    build_integrity_signature,
    cop_to_wompi_cents,
    verify_event_checksum,
)
from fastapi.testclient import TestClient

from tests.conftest import requires_db


def test_cop_to_wompi_cents():
    assert cop_to_wompi_cents(120_000) == 12_000_000


def test_integrity_signature_is_stable():
    from app.config import settings

    settings.wompi_integrity_secret = "test_integrity_secret"
    sig1 = build_integrity_signature("ref-123", 12_000_000)
    sig2 = build_integrity_signature("ref-123", 12_000_000)
    assert sig1 == sig2
    assert len(sig1) == 64


def test_wompi_api_base_sandbox():
    from app.config import settings

    settings.wompi_public_key = "pub_test_abc"
    settings.wompi_api_base_url = ""
    from app.application.billing.wompi_service import wompi_api_base

    assert wompi_api_base() == "https://sandbox.wompi.co/v1"


def test_sync_checkout_reference_mismatch():
    from unittest.mock import MagicMock, patch

    from app.application.billing.checkout_service import sync_checkout_with_wompi

    checkout = MagicMock()
    checkout.reference = "om-ref-1"
    db = MagicMock()

    with patch(
        "app.application.billing.checkout_service.fetch_transaction",
        return_value={"reference": "other-ref", "status": "APPROVED", "id": "tx-1"},
    ):
        assert sync_checkout_with_wompi(db, checkout=checkout, transaction_id="tx-1") is False


@requires_db
def test_return_from_wompi_page_without_id_confirms_by_reference(client, monkeypatch):
    import uuid

    from app.application.billing import checkout_service
    from app.config import settings

    monkeypatch.setattr(settings, "wompi_public_key", "pub_test_abc")
    monkeypatch.setattr(settings, "wompi_private_key", "prv_test_abc")
    monkeypatch.setattr(settings, "wompi_integrity_secret", "test_integrity_abc")
    tag = uuid.uuid4().hex[:8]
    reg = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Paga {tag}",
            "owner_name": "Dueña",
            "email": f"paga-{tag}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    session = client.post("/api/v1/billing/checkout", headers=headers, json={"plan_slug": "pro"}).json()
    ref = session["reference"]
    assert session["redirect_url"] == f"http://lvh.me:8000/api/v1/billing/wompi/return?ref={ref}", (
        "Wompi rechaza volver a localhost"
    )

    tampered = {"id": "tx-1", "status": "APPROVED", "reference": ref, "amount_in_cents": 100_000, "currency": "COP"}
    monkeypatch.setattr(checkout_service, "find_transaction_by_reference", lambda _ref: tampered)
    assert client.get(f"/api/v1/billing/checkout/{ref}", headers=headers).json()["status"] == "pending"

    paid = {**tampered, "id": "tx-2", "amount_in_cents": session["amount_in_cents"]}
    monkeypatch.setattr(checkout_service, "find_transaction_by_reference", lambda _ref: paid)
    status = client.get(f"/api/v1/billing/checkout/{ref}", headers=headers).json()
    assert status["status"] == "approved" and status["wompi_transaction_id"] == "tx-2"
    sub = client.get("/api/v1/subscriptions/me", headers=headers).json()
    assert sub["is_paid"] is True and sub["is_trial"] is False


@requires_db
def test_wompi_return_confirms_without_session_and_goes_back_to_pricing(client, monkeypatch):
    import uuid

    from app.application.billing import checkout_service
    from app.config import settings

    monkeypatch.setattr(settings, "app_public_url", "http://localhost:8000")
    monkeypatch.setattr(settings, "wompi_public_key", "pub_test_abc")
    monkeypatch.setattr(settings, "wompi_private_key", "prv_test_abc")
    monkeypatch.setattr(settings, "wompi_integrity_secret", "test_integrity_abc")
    monkeypatch.setattr(settings, "wompi_checkout_redirect_url", "http://localhost:4200/precios")
    tag = uuid.uuid4().hex[:8]
    reg = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Vuelve {tag}",
            "owner_name": "Dueña",
            "email": f"vuelve-{tag}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    session = client.post("/api/v1/billing/checkout", headers=headers, json={"plan_slug": "pro"}).json()
    ref = session["reference"]
    approved = {"id": "tx-7", "status": "APPROVED", "reference": ref, "amount_in_cents": session["amount_in_cents"], "currency": "COP"}
    monkeypatch.setattr(checkout_service, "fetch_transaction", lambda tx_id: approved if tx_id == "tx-7" else None)

    anonymous = TestClient(client.app)
    res = anonymous.get(f"/api/v1/billing/wompi/return?ref={ref}&id=tx-7&env=test", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == f"http://localhost:4200/precios?checkout=done&ref={ref}"
    assert client.get(f"/api/v1/billing/checkout/{ref}", headers=headers).json()["status"] == "approved"

    unknown = anonymous.get("/api/v1/billing/wompi/return?ref=no-existe&id=tx-7", follow_redirects=False)
    assert unknown.status_code == 303 and unknown.headers["location"] == "http://localhost:4200/precios"


def test_verify_event_checksum_valid():
    from app.config import settings

    settings.wompi_events_secret = "test_events_secret"
    event = {
        "event": "transaction.updated",
        "timestamp": 1530291411,
        "data": {
            "transaction": {
                "id": "tx-1",
                "status": "APPROVED",
                "amount_in_cents": 12_000_000,
            }
        },
        "signature": {
            "properties": [
                "transaction.id",
                "transaction.status",
                "transaction.amount_in_cents",
            ],
            "checksum": "PLACEHOLDER",
        },
    }
    parts = "tx-1APPROVED12000000" + "1530291411" + "test_events_secret"
    import hashlib

    checksum = hashlib.sha256(parts.encode("utf-8")).hexdigest().upper()
    event["signature"]["checksum"] = checksum
    assert verify_event_checksum(event, checksum) is True

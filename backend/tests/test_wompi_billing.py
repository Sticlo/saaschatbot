"""Wompi billing helpers."""

from __future__ import annotations

from app.application.billing.wompi_service import (
    build_integrity_signature,
    cop_to_wompi_cents,
    verify_event_checksum,
)


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

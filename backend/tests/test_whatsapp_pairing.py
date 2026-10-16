"""Vincular WhatsApp con código de 8 letras: el dueño que solo tiene el celular no puede escanear su propio QR."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import requires_db


def test_pairing_number_takes_colombian_numbers_as_typed():
    from app.application.whatsapp.whatsapp_service import pairing_number

    assert pairing_number("300 123 4567") == "573001234567"
    assert pairing_number("+57 300-123-4567") == "573001234567"
    assert pairing_number("+52 1 55 1234 5678") == "5215512345678"
    assert pairing_number("12345") is None
    assert pairing_number("") is None


def test_pairing_code_is_read_from_any_evolution_payload():
    from app.application.whatsapp.whatsapp_service import extract_pairing_code

    assert extract_pairing_code({"pairingCode": "WZYEH1YY", "base64": "x"}) == "WZYEH1YY"
    assert extract_pairing_code({"qrcode": {"pairingCode": "ABCD1234"}}) == "ABCD1234"
    assert extract_pairing_code({"qrcode": {"pairingCode": None, "base64": "x"}}) is None
    assert extract_pairing_code(None) is None


def _register(client: TestClient) -> tuple[dict, str]:
    auth = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": "Celular Solo",
            "owner_name": "Owner",
            "email": f"pair-{uuid.uuid4().hex[:8]}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    if auth.status_code == 503:
        pytest.skip("Plan no disponible")
    headers = {"Authorization": f"Bearer {auth.json()['access_token']}"}
    tenant_id = client.get("/api/v1/tenants/me", headers=headers).json()["id"]
    return headers, tenant_id


@requires_db
@patch("app.application.whatsapp.whatsapp_service.time.sleep", return_value=None)
@patch("app.application.whatsapp.whatsapp_service.gateway_fetch_instance", return_value=None)
@patch(
    "app.application.whatsapp.whatsapp_service.gateway_connection_state",
    return_value={"instance": {"state": "close"}},
)
@patch("app.application.whatsapp.whatsapp_service.gateway_connect")
@patch("app.application.whatsapp.whatsapp_service.gateway_ensure_realtime")
@patch("app.application.whatsapp.whatsapp_service.gateway_create_instance")
@patch("app.application.whatsapp.whatsapp_service.gateway_instance_exists", return_value=False)
def test_connect_with_phone_returns_the_pairing_code(
    _exists, mock_create, _realtime, mock_connect, _state, _fetch, _sleep, client: TestClient
):
    from app.application.whatsapp.whatsapp_service import pairing_code_for

    mock_connect.side_effect = [
        {"base64": "data:image/png;base64,abc"},
        {"pairingCode": "WZYEH1YY", "base64": "data:image/png;base64,abc"},
    ]
    headers, tenant_id = _register(client)

    response = client.post("/api/v1/whatsapp/connect", headers=headers, json={"phone": "300 123 4567"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "connecting"
    assert body["pairing_code"] == "WZYEH1YY"
    assert mock_create.call_args.kwargs["number"] == "573001234567"
    assert all(c.kwargs["number"] == "573001234567" for c in mock_connect.call_args_list)
    assert pairing_code_for(tenant_id) == "WZYEH1YY"


@requires_db
@patch("app.infrastructure.evolution.evolution_store.purge_instance_stored_data")
@patch("app.application.whatsapp.whatsapp_service.gateway_delete_instance")
@patch("app.application.whatsapp.whatsapp_service.gateway_logout_instance")
@patch(
    "app.application.whatsapp.whatsapp_service.gateway_fetch_instance",
    return_value={"connectionStatus": "connecting"},
)
@patch(
    "app.application.whatsapp.whatsapp_service.gateway_connection_state",
    return_value={"instance": {"state": "connecting"}},
)
@patch(
    "app.application.whatsapp.whatsapp_service.gateway_connect",
    return_value={"pairingCode": "ABCD1234"},
)
@patch("app.application.whatsapp.whatsapp_service.gateway_ensure_realtime")
@patch("app.application.whatsapp.whatsapp_service.gateway_ensure_webhook")
@patch("app.application.whatsapp.whatsapp_service.gateway_create_instance")
@patch("app.application.whatsapp.whatsapp_service.gateway_instance_exists", return_value=True)
def test_an_open_qr_connection_is_restarted_so_whatsapp_gives_a_code(
    _exists, mock_create, _webhook, _realtime, _connect, _state, _fetch, _logout, _delete, _purge,
    client: TestClient,
):
    headers, _ = _register(client)

    response = client.post("/api/v1/whatsapp/connect", headers=headers, json={"phone": "+57 300 123 4567"})

    assert response.status_code == 200
    assert response.json()["pairing_code"] == "ABCD1234"
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["number"] == "573001234567"


@requires_db
def test_a_wrong_number_is_rejected_before_touching_whatsapp(client: TestClient, monkeypatch):
    headers, _ = _register(client)

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("no debe tocar WhatsApp con un número inválido")

    monkeypatch.setattr("app.presentation.api.whatsapp.start_connection", _must_not_run)

    response = client.post("/api/v1/whatsapp/connect", headers=headers, json={"phone": "123"})
    assert response.status_code == 422
    assert "indicativo" in response.json()["detail"]


def _connecting(qr_age_seconds=None):
    from datetime import datetime, timedelta, timezone

    from app.domain.entities import Tenant, WhatsAppSession, WhatsAppStatus

    tenant = Tenant(id=uuid.uuid4(), business_name="x", slug="x", whatsapp_status=WhatsAppStatus.CONNECTING.value)
    session = WhatsAppSession(tenant_id=tenant.id, instance_name="t_x")
    if qr_age_seconds is not None:
        session.qr_base64 = "data:image/png;base64,abc"
        session.qr_updated_at = datetime.now(timezone.utc) - timedelta(seconds=qr_age_seconds)
    return tenant, session


def test_another_click_keeps_the_qr_the_owner_is_scanning():
    from app.application.whatsapp.whatsapp_service import _link_attempt_in_progress

    tenant, session = _connecting(qr_age_seconds=10)
    assert _link_attempt_in_progress(session, tenant, None)

    tenant, session = _connecting(qr_age_seconds=300)
    assert not _link_attempt_in_progress(session, tenant, None)

    tenant, session = _connecting()
    assert not _link_attempt_in_progress(session, tenant, None)


def test_another_click_keeps_the_code_only_for_the_same_number():
    from app.application.whatsapp.whatsapp_service import _link_attempt_in_progress, store_pairing_code

    tenant, session = _connecting(qr_age_seconds=10)
    store_pairing_code(tenant.id, "WZYEH1YY", "573001234567")

    assert _link_attempt_in_progress(session, tenant, "573001234567")
    assert not _link_attempt_in_progress(session, tenant, "573009999999")

    store_pairing_code(tenant.id, None)
    assert not _link_attempt_in_progress(session, tenant, "573001234567")


def test_a_failed_link_is_not_reused():
    from app.application.whatsapp.whatsapp_service import _link_attempt_in_progress
    from app.domain.entities import WhatsAppStatus

    tenant, session = _connecting(qr_age_seconds=10)
    tenant.whatsapp_status = WhatsAppStatus.DISCONNECTED.value
    assert not _link_attempt_in_progress(session, tenant, None)


def test_new_code_from_whatsapp_reaches_the_panel():
    from app.application.messaging.message_service import handle_qrcode_update
    from app.application.whatsapp.whatsapp_service import pairing_code_for
    from app.domain.entities import Tenant, WhatsAppSession

    tenant = Tenant(id=uuid.uuid4(), business_name="x", slug="x")
    session = WhatsAppSession(tenant_id=tenant.id, instance_name="t_x")
    data = {"qrcode": {"pairingCode": "QWER5678", "code": "2@abc", "base64": "data:image/png;base64,abc"}}

    with patch("app.application.realtime.realtime_service.publish_panel_event") as mock_publish:
        handle_qrcode_update(MagicMock(), session, tenant, data)

    assert pairing_code_for(tenant.id) == "QWER5678"
    assert mock_publish.call_args.args[1]["pairing_code"] == "QWER5678"

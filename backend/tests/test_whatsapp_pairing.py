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


def test_recreate_waits_until_evolution_frees_the_name(monkeypatch):
    """Evolution responde 403 si se crea la instancia justo después de borrarla: el primer intento fallaba."""
    from app.application.whatsapp import whatsapp_service as svc
    from app.application.whatsapp.whatsapp_gateway import WhatsAppGatewayError

    monkeypatch.setattr(svc, "RECREATE_RETRY_WAITS", (0, 0, 0))
    monkeypatch.setattr(svc.settings, "evolution_database_url", "")
    monkeypatch.setattr(svc, "gateway_logout_instance", MagicMock())
    monkeypatch.setattr(svc, "gateway_delete_instance", MagicMock())
    monkeypatch.setattr(svc, "gateway_instance_exists", MagicMock(return_value=False))
    create = MagicMock(
        side_effect=[WhatsAppGatewayError("name in use", status_code=403), WhatsAppGatewayError("name in use", status_code=403), {}]
    )
    monkeypatch.setattr(svc, "gateway_create_instance", create)
    session = MagicMock(instance_name="t_test")

    svc._reset_gateway_instance_credentials(session, webhook_url="http://x", webhook_secret="s")
    assert create.call_count == 3

    create.reset_mock(side_effect=True)
    create.side_effect = WhatsAppGatewayError("name in use", status_code=403)
    with pytest.raises(WhatsAppGatewayError):
        svc._reset_gateway_instance_credentials(session, webhook_url="http://x", webhook_secret="s")
    assert create.call_count == 4


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


def test_only_a_401_right_after_the_code_counts_as_whatsapp_rejecting_the_link():
    from datetime import datetime, timedelta, timezone

    from app.application.whatsapp.whatsapp_service import is_rejected_link

    _, session = _connecting(qr_age_seconds=20)
    assert is_rejected_link(session, 401)
    assert not is_rejected_link(session, 408)

    session.last_connected_at = datetime.now(timezone.utc)
    assert not is_rejected_link(session, 401)

    _, session = _connecting(qr_age_seconds=3600)
    assert not is_rejected_link(session, 401)

    _, session = _connecting()
    assert not is_rejected_link(session, 401)

    _, session = _connecting(qr_age_seconds=20)
    session.last_connected_at = datetime.now(timezone.utc) - timedelta(days=4)
    assert is_rejected_link(session, 401)


def test_our_own_logout_before_a_new_code_is_not_a_rejection():
    from app.application.whatsapp.whatsapp_service import is_rejected_link, mark_own_logout

    _, session = _connecting(qr_age_seconds=20)
    mark_own_logout(session.tenant_id)
    assert not is_rejected_link(session, 401)


def _close_from_whatsapp(session, tenant, data):
    from app.application.messaging.message_service import handle_connection_update

    conv = "app.application.conversations.whatsapp_conversation_service"
    with patch(f"{conv}.should_ignore_stale_open_session", return_value=False), patch(
        f"{conv}.needs_new_whatsapp_binding", return_value=False
    ), patch(f"{conv}.ensure_whatsapp_binding_ready"), patch(
        "app.application.realtime.realtime_service.publish_panel_event"
    ) as mock_publish:
        handle_connection_update(MagicMock(), tenant=tenant, session=session, data=data)
    return mock_publish.call_args.args[1]


def test_panel_learns_that_whatsapp_rejected_the_code():
    from app.application.whatsapp.whatsapp_service import (
        link_error_for,
        pairing_code_for,
        set_link_error,
        store_pairing_code,
    )

    tenant, session = _connecting(qr_age_seconds=20)
    store_pairing_code(tenant.id, "WZYEH1YY", "573001234567")

    event = _close_from_whatsapp(session, tenant, {"state": "close", "statusReason": 401})

    assert event["status"] == "disconnected"
    assert event["link_error"] == "rejected"
    assert link_error_for(tenant.id) == "rejected"
    assert pairing_code_for(tenant.id) is None
    set_link_error(tenant.id, None)


def test_a_rejected_link_keeps_the_exact_reason_and_alerts_the_dev():
    from app.application.whatsapp.whatsapp_service import record_link_rejection, store_pairing_code

    tenant, session = _connecting(qr_age_seconds=20)
    tenant.business_name = "Rin Eventos"
    store_pairing_code(tenant.id, "WZYEH1YY", "573001234567")
    db = MagicMock()
    db.query.return_value.filter.return_value.count.return_value = 4
    evolution = {
        "code": 401,
        "message": "Invalid account signature",
        "data": {"omitel": "pairing-error", "reason": "Invalid account signature", "accountType": 1},
    }

    with patch(
        "app.infrastructure.evolution.evolution_store.fetch_disconnection_detail", return_value=evolution
    ), patch("app.application.billing.tenant_service.log_audit") as mock_audit, patch(
        "app.application.monitoring.dev_alerts.alert_dev"
    ) as mock_alert:
        details = record_link_rejection(db, tenant, session, 401)

    assert details["reason"] == "Invalid account signature"
    assert details["account_type"] == 1
    assert details["baileys_error"] is True
    assert details["mode"] == "codigo"
    assert details["attempts_24h"] == 4
    assert mock_audit.call_args.kwargs["action"] == "whatsapp.link_rejected"
    alert_detail = mock_alert.call_args.args[2]
    assert "Invalid account signature" in alert_detail
    assert "patch-baileys-hosted.js" in alert_detail
    store_pairing_code(tenant.id, None)


def test_a_plain_whatsapp_cut_is_reported_without_a_baileys_error():
    from app.application.whatsapp.whatsapp_service import record_link_rejection

    tenant, session = _connecting(qr_age_seconds=20)
    tenant.business_name = "x"
    evolution = {"code": 401, "message": "Connection Failure", "data": {"reason": "401", "location": "lla"}}

    with patch(
        "app.infrastructure.evolution.evolution_store.fetch_disconnection_detail", return_value=evolution
    ), patch("app.application.billing.tenant_service.log_audit"), patch(
        "app.application.monitoring.dev_alerts.alert_dev"
    ) as mock_alert:
        details = record_link_rejection(MagicMock(), tenant, session, 401)

    assert details["reason"] == "Connection Failure"
    assert details["baileys_error"] is False
    assert details["whatsapp_location"] == "lla"
    assert "límite de vinculaciones" in mock_alert.call_args.args[2]
    assert details["mode"] == "qr"
    assert mock_alert.called


def test_a_network_drop_while_linking_is_not_reported_as_rejected():
    from app.application.whatsapp.whatsapp_service import link_error_for

    tenant, session = _connecting(qr_age_seconds=20)

    event = _close_from_whatsapp(session, tenant, {"state": "close", "statusReason": 408})

    assert event["link_error"] is None
    assert link_error_for(tenant.id) is None

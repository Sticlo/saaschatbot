"""Con medios de pago la IA cierra la venta sola y le avisa al dueño del pedido y del pago."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application.ai.ai_qualify_service import build_qualify_system_prompt
from app.application.ai.ai_shortcut_service import (
    AiGeneratedReply,
    OrderSummary,
    parse_ai_reply,
    reply_format_instruction,
)
from tests.conftest import requires_db
from tests.test_phase1 import _register
from tests.test_sales_stage_and_booking import OWNER_ONLY, _run, _seed

PAYMENTS = "Nequi 300 123 4567 a nombre de Laura Gómez; contra entrega en Medellín"
ORDER = {
    "items": "Tenis blancos talla 38",
    "total": "$180.000",
    "customer": "Laura Gómez",
    "delivery": "Cra 45 #10-20, Laureles, Medellín",
    "payment": "Nequi",
    "paid": False,
}


def test_order_is_read_from_the_reply():
    raw = json.dumps({"message": "¡Listo!", "stage": "cierre", "order": ORDER})
    order = parse_ai_reply(raw, valid_ids=frozenset()).order
    assert order == OrderSummary(
        items="Tenis blancos talla 38",
        total="$180.000",
        customer="Laura Gómez",
        delivery="Cra 45 #10-20, Laureles, Medellín",
        payment="Nequi",
        paid=False,
    )

    paid = json.dumps({"message": "¡Gracias!", "order": {**ORDER, "paid": "true", "total": None}})
    order = parse_ai_reply(paid, valid_ids=frozenset()).order
    assert order.paid is True and order.total == ""

    for empty in (None, "null", {"items": ""}, {"total": "$10"}, ["x"]):
        raw = json.dumps({"message": "Hola", "order": empty})
        assert parse_ai_reply(raw, valid_ids=frozenset()).order is None


def test_reply_format_only_asks_for_orders_when_the_business_sells():
    selling = reply_format_instruction(has_shortcuts=False, booking_enabled=False, selling=True)
    assert '"order":null' in selling
    assert "cierra tú la venta" in selling
    assert "Nunca inventes números de" in selling
    assert '{"items":"2 camisetas' in selling

    handoff = reply_format_instruction(has_shortcuts=False, booking_enabled=False)
    assert "order" not in handoff
    assert "No pidas datos de pago" in handoff


def test_prompt_closes_only_with_payment_methods():
    tenant = SimpleNamespace(business_name="KatShoes")

    def profile(**kw):
        base = dict(ai_system_prompt=None, payment_methods=None, onboarding_answers={})
        base.update(kw)
        return SimpleNamespace(**base)

    assert "NO cierres la venta" in build_qualify_system_prompt(tenant, profile())
    selling = build_qualify_system_prompt(tenant, profile(payment_methods=PAYMENTS))
    assert "cierra tú la venta" in selling and "NO cierres" not in selling

    custom = build_qualify_system_prompt(tenant, profile(ai_system_prompt="Eres de KatShoes.", payment_methods=PAYMENTS))
    assert "cierra tú la venta" in custom and "NO cierres" not in custom


@patch("app.application.ai.ai_conversation_service.chat_completion")
def test_payment_methods_reach_the_model(mock_chat):
    from app.application.ai.ai_conversation_service import generate_qualify_reply

    mock_chat.return_value = json.dumps({"message": "Hola", "stage": "conversando", "order": None})
    tenant = SimpleNamespace(business_name="KatShoes")
    profile = SimpleNamespace(ai_system_prompt="Eres de KatShoes.", payment_methods=PAYMENTS, onboarding_answers={})
    generate_qualify_reply(tenant=tenant, profile=profile, contact_name="Laura", history=[])
    system = mock_chat.call_args.args[0][0]["content"]
    assert "Cómo pagarle al negocio" in system
    assert PAYMENTS in system
    assert '"order":null' in system


def _seed_selling(db, body: str):
    return _seed(db, body=body, profile_kwargs={"payment_methods": PAYMENTS}, alert_recipients=OWNER_ONLY)


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_closes_the_sale_without_handing_off(_sleep, mock_generate, mock_send, mock_alert):
    from app.application.ai.ai_service import _was_handed_off
    from app.infrastructure.persistence.database import SessionLocal

    mock_generate.return_value = AiGeneratedReply(
        message="¡Listo, Laura! Te quedan en $180.000. Puedes pagar por Nequi al 300 123 4567 🙌",
        stage="cierre",
        order=OrderSummary(**ORDER),
    )
    with SessionLocal() as db:
        tenant, conv, msg = _seed_selling(db, "Me llevo los blancos en talla 38, envíalos a Laureles")
        assert _run(db, tenant, conv, msg) is True
        conv_id = conv.id

        assert conv.interest_status == "interested"
        assert conv.mode == "auto" and conv.ai_active is True
        assert _was_handed_off(conv_id) is False
        texts = [c.args[2] for c in mock_alert.call_args_list]
        assert len(texts) == 1
        assert "la IA cerró un pedido de Laura" in texts[0]
        assert "• Pedido: Tenis blancos talla 38" in texts[0]
        assert "• Total: $180.000" in texts[0]
        assert "• Entrega: Cra 45 #10-20, Laureles, Medellín" in texts[0]
        assert "pendiente del comprobante" in texts[0]
        assert "ya quiere comprar" not in texts[0]

        # La IA repite el pedido en sus respuestas: el dueño no recibe el mismo aviso otra vez.
        assert _run(db, tenant, conv, msg) is True
        assert mock_alert.call_count == 1

        mock_generate.return_value = AiGeneratedReply(
            message="¡Recibido, gracias! Tu pedido quedó registrado ✅",
            stage="cierre",
            order=OrderSummary(**{**ORDER, "paid": True}),
        )
        assert _run(db, tenant, conv, msg) is True

    assert mock_alert.call_count == 2
    paid_text = mock_alert.call_args.args[2]
    assert "¡pedido pagado! Laura mandó el comprobante" in paid_text
    assert "• Pago por Nequi: revisa que te haya llegado antes de despachar" in paid_text


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_that_needs_the_owner_still_hands_off(_sleep, mock_generate, _send, mock_alert):
    from app.infrastructure.persistence.database import SessionLocal

    mock_generate.return_value = AiGeneratedReply(
        message="¡De una! El envío a Pasto te confirmo por aquí apenas lo revise 🙌", stage="cierre"
    )
    with SessionLocal() as db:
        tenant, conv, msg = _seed_selling(db, "Me llevo los blancos, ¿cuánto vale el envío a Pasto?")
        assert _run(db, tenant, conv, msg) is True

    assert "ya quiere comprar" in mock_alert.call_args.args[2]


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_without_payment_methods_the_owner_still_closes(_sleep, mock_generate, _send, mock_alert):
    from app.infrastructure.persistence.database import SessionLocal

    mock_generate.return_value = AiGeneratedReply(
        message="¡De una! Por aquí mismo te confirmo 🙌", stage="cierre", order=OrderSummary(**ORDER)
    )
    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="Me llevo los blancos en talla 38", alert_recipients=OWNER_ONLY)
        assert _run(db, tenant, conv, msg) is True

    texts = [c.args[2] for c in mock_alert.call_args_list]
    assert len(texts) == 1 and "ya quiere comprar" in texts[0]


@requires_db
def test_business_profile_saves_payment_methods(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    r = client.put(
        "/api/v1/outbound/business-profile",
        json={"industry": "Tienda de zapatos", "payment_methods": f"  {PAYMENTS} "},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["payment_methods"] == PAYMENTS
    assert f"Cómo le pagan: {PAYMENTS}" in r.json()["ai_summary"]

    again = client.get("/api/v1/outbound/business-profile", headers=headers).json()
    assert again["payment_methods"] == PAYMENTS

    r = client.put("/api/v1/outbound/business-profile", json={"payment_methods": ""}, headers=headers)
    assert r.json()["payment_methods"] is None

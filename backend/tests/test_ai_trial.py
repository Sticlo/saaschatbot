from __future__ import annotations

import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.conftest import requires_db
from tests.test_phase1 import _register


def _product(name, *, photos=(), details=""):
    return SimpleNamespace(id=uuid.uuid4(), name=name, photos=list(photos), details=details, price_cop=60000)


def _keys(tips):
    return [t["key"] for t in tips]


def test_tips_catch_what_went_wrong_at_alfa_store():
    from app.application.ai.ai_readiness_service import sales_tips

    tips = sales_tips(
        answers={"delivery_info": "Envíos a todo el país por Interrapidísimo"},
        products=[_product("Ropa Unisex", photos=["a.jpg"]), _product("Buzos hombre", photos=["b.jpg"])],
        recipients=[{"phone": "+573001234567"}],
        own_number=True,
        booking=True,
        tried=False,
    )
    assert _keys(tips) == ["alerts-own", "catalog", "booking", "try"]
    assert "«Ropa Unisex» es una categoría" in tips[1]["hint"]
    assert tips[2]["action"] == "booking-off"


def test_a_well_set_up_store_gets_no_tips():
    from app.application.ai.ai_readiness_service import sales_tips

    products = [
        _product("Buzo negro oversize", photos=["a.jpg"], details="Tallas S a XL"),
        _product("Camiseta básica blanca", photos=["b.jpg"], details="Tallas S a XL"),
        _product("Jogger gris", photos=["c.jpg"], details="Tallas 28 a 36"),
        _product("Gorra trucker", photos=["d.jpg"], details="Talla única"),
    ]
    tips = sales_tips(
        answers={"delivery_info": "Envíos a todo el país"},
        products=products,
        recipients=[{"phone": "+573009998877"}],
        own_number=False,
        booking=False,
        tried=True,
    )
    assert tips == []


def test_tips_point_at_missing_alerts_photos_and_details():
    from app.application.ai.ai_readiness_service import sales_tips

    products = [_product(f"Torta de {s}") for s in ("chocolate", "vainilla", "limón", "fresa")]
    tips = sales_tips(answers={}, products=products, recipients=[], own_number=False, booking=False, tried=True)
    assert _keys(tips) == ["alerts", "photos", "details"]
    assert tips[1]["label"] == "4 productos sin foto"


@requires_db
def test_try_ai_shows_the_photos_and_never_touches_real_chats(client: TestClient, monkeypatch):
    from app.application.ai import ai_trial_service
    from app.application.ai.ai_shortcut_service import AiGeneratedReply
    from app.domain.entities import Message, Product
    from app.infrastructure.persistence.database import SessionLocal

    seen: dict = {}

    def fake_reply(**kw):
        seen.update(kw)
        return AiGeneratedReply(message="¡Claro! Mira 👇", missing="si hay talla XXL")

    monkeypatch.setattr(ai_trial_service, "generate_reply", fake_reply)
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    tenant_id = uuid.UUID(client.get("/api/v1/tenants/me", headers=headers).json()["id"])
    with SessionLocal() as db:
        db.add(Product(tenant_id=tenant_id, name="Buzo negro", price_cop=60000, photos=[f"assets/{tenant_id}/buzo.jpg"]))
        db.add(Product(tenant_id=tenant_id, name="Gorra", price_cop=30000, photos=[f"assets/{tenant_id}/gorra.jpg"]))
        db.commit()
        before = db.query(Message).filter(Message.tenant_id == tenant_id).count()

    tips = client.get("/api/v1/outbound/business-profile", headers=headers).json()["tips"]
    assert "try" in _keys(tips)

    r = client.post(
        "/api/v1/outbound/try-ai",
        json={"turns": [
            {"from": "client", "text": "Hola, ¿tienen buzos?"},
            {"from": "ai", "text": "¡Sí! Tenemos el Buzo negro a $60.000."},
            {"from": "client", "text": "Fotos"},
        ]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "¡Claro! Mira 👇"
    assert body["photos"] == ["/api/v1/outbound/assets/buzo.jpg"]
    assert "si hay talla XXL" in body["notes"][0]
    assert [m.body for m in seen["history"]][-1] == "Fotos"

    with SessionLocal() as db:
        assert db.query(Message).filter(Message.tenant_id == tenant_id).count() == before
    tips = client.get("/api/v1/outbound/business-profile", headers=headers).json()["tips"]
    assert "try" not in _keys(tips)


@requires_db
def test_try_ai_needs_a_client_message(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    r = client.post("/api/v1/outbound/try-ai", json={"turns": [{"from": "ai", "text": "Hola"}]}, headers=headers)
    assert r.status_code == 422
    assert r.json()["detail"] == "Escribe un mensaje como si fueras un cliente."

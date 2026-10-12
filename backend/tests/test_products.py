"""Productos: la IA sabe qué hay, a qué precio, y descuenta lo que vende."""
from __future__ import annotations

import json
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application.ai.ai_shortcut_service import AiGeneratedReply, OrderSummary, parse_ai_reply
from app.application.products.product_service import parse_catalog_lines, products_prompt_block
from app.domain.entities import Product
from tests.conftest import requires_db
from tests.test_ai_closes_sale import PAYMENTS
from tests.test_phase1 import _register
from tests.test_sales_stage_and_booking import OWNER_ONLY, _run, _seed

CATALOG_TEXT = """KatShoes — catálogo 2026
- Tenis blancos — $180.000 — tallas 36 a 40, cuero
- Botas negras — $250.000
Sandalias playa | 45.000 | colores: rojo, azul
Horario — lun a sáb 9am-7pm
Envíos a todo el país
"""


def test_catalog_lines_become_products():
    assert parse_catalog_lines(CATALOG_TEXT) == [
        {"name": "Tenis blancos", "price_cop": 180000, "details": "tallas 36 a 40, cuero"},
        {"name": "Botas negras", "price_cop": 250000, "details": None},
        {"name": "Sandalias playa", "price_cop": 45000, "details": "colores: rojo, azul"},
    ]


def _product(name, price=None, stock=None, details=None):
    return Product(id=uuid.uuid4(), name=name, price_cop=price, stock=stock, details=details)


def test_prompt_lists_availability():
    tenis = _product("Tenis blancos", 180000, 3, "tallas 36 a 40")
    block = products_prompt_block(
        [tenis, _product("Botas negras", 250000, 0), _product("Gorra")], selling=True
    )
    assert f"ref={tenis.id.hex[:8]} | Tenis blancos | $180.000 | quedan 3 | tallas 36 a 40" in block
    assert "| Botas negras | $250.000 | AGOTADO" in block
    assert "| Gorra | precio por confirmar | disponible" in block
    assert '"lines"' in block
    assert '"lines"' not in products_prompt_block([tenis], selling=False)
    assert products_prompt_block([], selling=True) == ""


def test_order_lines_are_read():
    raw = json.dumps(
        {
            "message": "¡Listo!",
            "order": {
                "items": "2 tenis",
                "lines": [{"ref": "A1B2C3D4", "qty": 2}, {"ref": "zz", "qty": 1}, {"ref": "a1b2c3d4", "qty": 0}],
            },
        }
    )
    assert parse_ai_reply(raw, valid_ids=frozenset()).order.lines == (("a1b2c3d4", 2),)


@requires_db
def test_products_api_and_catalog_import(client: TestClient):
    from app.domain.entities import TenantProfile
    from app.infrastructure.persistence.database import SessionLocal

    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    r = client.post("/api/v1/products/import-catalog", headers=headers)
    assert r.status_code == 422

    with SessionLocal() as db:
        profile = db.query(TenantProfile).filter(TenantProfile.tenant_id == uuid.UUID(auth["tenant_id"])).one()
        profile.quick_shortcuts = [
            {"id": "c1", "label": "Catálogo", "type": "document", "file_path": "x.pdf", "content": CATALOG_TEXT}
        ]
        db.commit()

    r = client.post("/api/v1/products", json={"name": "Tenis blancos", "price_cop": 175000, "stock": 2}, headers=headers)
    assert r.status_code == 201, r.text
    tenis_id = r.json()["id"]

    r = client.post("/api/v1/products/import-catalog", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2
    names = [p["name"] for p in r.json()["products"]]
    assert names == ["Tenis blancos", "Botas negras", "Sandalias playa"]
    assert r.json()["products"][0]["price_cop"] == 175000

    r = client.patch(f"/api/v1/products/{tenis_id}", json={"stock": None}, headers=headers)
    assert r.status_code == 200 and r.json()["stock"] is None and r.json()["price_cop"] == 175000
    r = client.patch(f"/api/v1/products/{tenis_id}", json={"stock": -1}, headers=headers)
    assert r.status_code == 422

    assert client.delete(f"/api/v1/products/{tenis_id}", headers=headers).status_code == 204
    assert len(client.get("/api/v1/products", headers=headers).json()) == 2

    other = _register(client)
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.get("/api/v1/products", headers=other_headers).json() == []
    botas_id = client.get("/api/v1/products", headers=headers).json()[0]["id"]
    assert client.patch(f"/api/v1/products/{botas_id}", json={"stock": 1}, headers=other_headers).status_code == 404


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_sold_units_are_discounted_once(_sleep, mock_generate, _send, mock_alert):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, msg = _seed(
            db,
            body="Me llevo 2 tenis blancos talla 38",
            profile_kwargs={"payment_methods": PAYMENTS},
            alert_recipients=OWNER_ONLY,
        )
        tenis = Product(tenant_id=tenant.id, name="Tenis blancos", price_cop=180000, stock=3)
        db.add(tenis)
        db.commit()
        ref = tenis.id.hex[:8]

        mock_generate.return_value = AiGeneratedReply(
            message="¡Listo! Son $360.000. Pagas por Nequi al 300 123 4567 🙌",
            stage="cierre",
            order=OrderSummary(items="2 Tenis blancos talla 38", total="$360.000", lines=((ref, 2),)),
        )
        assert _run(db, tenant, conv, msg) is True
        assert mock_generate.call_args.kwargs["products"][0].name == "Tenis blancos"
        db.refresh(tenis)
        assert tenis.stock == 1
        assert "• Quedan 1 de Tenis blancos" in mock_alert.call_args.args[2]

        # La IA repite el pedido: no se descuenta otra vez.
        assert _run(db, tenant, conv, msg) is True
        db.refresh(tenis)
        assert tenis.stock == 1

        tenant2, conv2, msg2 = _seed(
            db, body="Quiero 5 tenis", profile_kwargs={"payment_methods": PAYMENTS}, alert_recipients=OWNER_ONLY
        )
        tenis.tenant_id = tenant2.id
        db.commit()
        mock_generate.return_value = AiGeneratedReply(
            message="Solo me queda 1 🙌", stage="cierre", order=OrderSummary(items="1 Tenis", lines=((ref, 5),))
        )
        assert _run(db, tenant2, conv2, msg2) is True
        db.refresh(tenis)
        assert tenis.stock == 0
        assert "⚠️ Se agotó Tenis blancos" in mock_alert.call_args.args[2]

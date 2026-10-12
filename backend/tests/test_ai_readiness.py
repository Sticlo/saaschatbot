"""La IA no queda configurada a medias: sin lo mínimo no se prende, y lo que no sabe se le enseña una vez."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application.ai.ai_readiness_service import readiness_from
from app.application.ai.ai_shortcut_service import AiGeneratedReply, parse_ai_reply
from tests.conftest import requires_db
from tests.test_ai_closes_sale import PAYMENTS
from tests.test_phase1 import _register
from tests.test_sales_stage_and_booking import OWNER_ONLY, _run, _seed

COMPLETE = {
    "products_services": "Tenis y botas para mujer",
    "price_range": "Tenis desde $150.000",
    "payment_methods": PAYMENTS,
    "delivery_info": "Envíos a todo Colombia por Interrapidísimo, $15.000, llegan en 2-3 días",
    "location_hours": "Medellín, Laureles — lun a sáb 9am-7pm",
}


TENIS = SimpleNamespace(name="Tenis blancos", price_cop=150000)


def _item(readiness: dict, key: str) -> dict:
    return next(i for i in readiness["items"] if i["key"] == key)


def _missing(answers: dict, products: list | None = None) -> list[str]:
    return [i["key"] for i in readiness_from(answers, [TENIS] if products is None else products)["items"] if not i["done"]]


def test_readiness_lists_what_is_missing():
    assert _missing({}, []) == ["products", "prices", "payments", "delivery", "location"]
    assert readiness_from(COMPLETE, [TENIS])["ready"] is True

    # «Nequi» solo no le sirve al cliente para pagar.
    vague = readiness_from({**COMPLETE, "payment_methods": "Nequi"}, [TENIS])
    assert vague["ready"] is False and "300 123 4567" in _item(vague, "payments")["hint"]
    assert _missing({**COMPLETE, "payment_methods": "Efectivo o contra entrega"}) == []


def test_prices_come_from_the_inventory():
    # Un rango en el formulario no alcanza para cotizar: los precios salen del inventario.
    assert _missing(COMPLETE, []) == ["prices"]
    assert _missing({**COMPLETE, "products_services": "", "price_range": ""}) == []

    botas = SimpleNamespace(name="Botas negras", price_cop=None)
    readiness = readiness_from(COMPLETE, [TENIS, botas])
    assert readiness["ready"] is False
    assert _item(readiness, "prices")["hint"].startswith("Sin precio: Botas negras.")

    # Con el catálogo ya subido, se ofrece importarlo en vez de teclear todo.
    imported = _item(readiness_from(COMPLETE, [], has_catalog=True), "prices")
    assert imported["action"] == "import-catalog" and "impórtalo" in imported["hint"]
    assert "action" not in _item(readiness_from(COMPLETE, []), "prices")


def test_inventory_price_wins_over_the_catalog():
    from app.application.products.product_service import products_prompt_block

    product = SimpleNamespace(id=uuid.uuid4(), name="Tenis blancos", price_cop=150000, stock=None, details=None, photos=[])
    block = products_prompt_block([product], selling=True)
    assert "Tenis blancos | $150.000" in block
    assert "cotiza SIEMPRE con el precio de esta lista" in block


def test_missing_info_is_read_from_the_reply():
    raw = json.dumps({"message": "Te confirmo por aquí", "missing": " costo del envío a Cali. "})
    assert parse_ai_reply(raw, valid_ids=frozenset()).missing == "costo del envío a Cali"
    for empty in ("", None, "null", "ninguno", 3):
        raw = json.dumps({"message": "Hola", "missing": empty})
        assert parse_ai_reply(raw, valid_ids=frozenset()).missing == ""


@requires_db
def test_ai_cannot_be_turned_on_half_configured(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    profile = client.get("/api/v1/outbound/business-profile", headers=headers).json()
    assert profile["readiness"]["ready"] is False

    assert client.patch("/api/v1/tenants/me", json={"ai_global_enabled": False}, headers=headers).status_code == 200
    r = client.patch("/api/v1/tenants/me", json={"ai_global_enabled": True}, headers=headers)
    assert r.status_code == 409
    assert "envíos o entregas" in r.json()["detail"]

    r = client.put("/api/v1/outbound/business-profile", json=COMPLETE, headers=headers)
    assert r.status_code == 200
    assert [i["key"] for i in r.json()["readiness"]["items"] if not i["done"]] == ["prices"]
    assert client.patch("/api/v1/tenants/me", json={"ai_global_enabled": True}, headers=headers).status_code == 409

    r = client.post("/api/v1/products", json={"name": "Tenis blancos", "price_cop": 150000}, headers=headers)
    assert r.status_code == 201
    r = client.get("/api/v1/outbound/business-profile", headers=headers)
    assert r.json()["readiness"]["ready"] is True
    assert r.json()["delivery_info"] == COMPLETE["delivery_info"]
    assert "Envíos y entregas: Envíos a todo Colombia" in r.json()["ai_system_prompt"]

    r = client.patch("/api/v1/tenants/me", json={"ai_global_enabled": True}, headers=headers)
    assert r.status_code == 200 and r.json()["ai_global_enabled"] is True


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_unknown_question_is_noted_and_learned(_sleep, mock_generate, _send, mock_alert):
    from app.application.ai.ai_knowledge_service import answer_question
    from app.domain.entities import AiQuestion
    from app.infrastructure.persistence.database import SessionLocal

    mock_generate.return_value = AiGeneratedReply(
        message="¡Claro que enviamos! El costo a Cali te lo confirmo por aquí apenas lo revise.",
        stage="interesado",
        missing="costo del envío a Cali",
    )
    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="¿Cuánto vale el envío a Cali?", alert_recipients=OWNER_ONLY)
        assert _run(db, tenant, conv, msg) is True
        assert _run(db, tenant, conv, msg) is True
        tenant_id = tenant.id
        rows = db.query(AiQuestion).filter(AiQuestion.tenant_id == tenant_id).all()
        assert len(rows) == 1
        assert rows[0].topic == "costo del envío a Cali"
        assert rows[0].question == "¿Cuánto vale el envío a Cali?"
        assert rows[0].times_asked == 2

        # Un solo aviso al dueño, con lo que faltó.
        texts = [c.args[2] for c in mock_alert.call_args_list]
        assert len(texts) == 1
        assert "la IA no tenía este dato: *costo del envío a Cali*" in texts[0]

        # Respondida una vez, la IA la recibe en las siguientes respuestas.
        answer_question(rows[0], "A Cali vale $15.000 y llega en 2 días")
        db.commit()
        mock_generate.return_value = AiGeneratedReply(message="A Cali vale $15.000 🙌", stage="interesado")
        assert _run(db, tenant, conv, msg) is True

    knowledge = mock_generate.call_args.kwargs["knowledge"]
    assert "- costo del envío a Cali: A Cali vale $15.000 y llega en 2 días" in knowledge
    assert mock_alert.call_count == 1


@requires_db
def test_questions_api_is_per_business(client: TestClient):
    import uuid

    from app.application.ai.ai_knowledge_service import record_missing_info
    from app.infrastructure.persistence.database import SessionLocal

    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    other = _register(client)
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    tenant_id = uuid.UUID(client.get("/api/v1/tenants/me", headers=headers).json()["id"])
    with SessionLocal() as db:
        record_missing_info(db, tenant_id=tenant_id, conversation_id=None, topic="si hay talla 42", question="¿Hay 42?")
        db.commit()

    items = client.get("/api/v1/ai-questions", headers=headers).json()
    assert [(q["topic"], q["status"]) for q in items] == [("si hay talla 42", "open")]
    qid = items[0]["id"]
    assert client.get("/api/v1/ai-questions", headers=other_headers).json() == []
    assert client.put(f"/api/v1/ai-questions/{qid}", json={"answer": "x"}, headers=other_headers).status_code == 404

    r = client.put(f"/api/v1/ai-questions/{qid}", json={"answer": "Solo hasta la 40"}, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "answered"
    assert client.delete(f"/api/v1/ai-questions/{qid}", headers=headers).status_code == 204
    assert client.get("/api/v1/ai-questions", headers=headers).json() == []

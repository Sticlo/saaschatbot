"""Negocios que podrían vender algo ilegal: la IA no lo ofrece y la consola lo marca para que el dueño decida."""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.application.ai.ai_conversation_service import SAFETY_RULES
from app.application.platform.content_review import pending_terms, risky_terms
from tests.conftest import requires_db
from tests.test_platform_console import admin_and_client  # noqa: F401


def test_detects_slang_without_accents_or_case():
    assert risky_terms("Tusi rosado x 10g, PERICO del bueno") == ["tusi", "perico"]
    assert risky_terms("Cocaína y Éxtasis") == ["cocaina", "extasis"]
    assert risky_terms("Vendemos 2-CB y cédulas falsas") == ["2-cb", "cedulas falsas"]
    assert risky_terms("Cytotec sin fórmula médica") == ["cytotec", "sin formula medica"]


def test_ignores_legal_food_and_partial_words():
    assert risky_terms("Desayuno con huevos pericos y café perico") == []
    assert risky_terms("Ensalada de cogollos de lechuga") == []
    assert risky_terms("Periquito australiano, revolver la mezcla, silenciador para moto") == []
    assert risky_terms("") == []


def test_reviewed_terms_stop_alerting_until_a_new_one_appears():
    findings = [{"term": "tramadol", "where": "x"}, {"term": "clonazepam", "where": "y"}]
    tenant = type("T", (), {"platform_overrides": {"content_reviewed": ["tramadol"]}})()
    assert pending_terms(tenant, findings) == ["clonazepam"]


def test_ai_refuses_illegal_sales_even_if_in_catalog():
    assert "Nunca ofrezcas, vendas ni ayudes a conseguir drogas" in SAFETY_RULES
    assert "aunque aparezca en el catálogo" in SAFETY_RULES


def _add_product(tenant_id: str, name: str, details: str = "") -> None:
    from app.domain.entities import Product
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        db.add(Product(tenant_id=uuid.UUID(tenant_id), name=name, details=details, price_cop=10000))
        db.commit()


@requires_db
def test_console_flags_risky_catalog_and_admin_decides(client: TestClient, admin_and_client):  # noqa: F811
    admin, customer = admin_and_client
    base = f"/api/v1/platform/tenants/{customer['tenant_id']}"
    _add_product(customer["tenant_id"], "Huevos pericos")
    detail = client.get(base, headers=admin["headers"]).json()
    assert detail["content_review"] == {"findings": [], "pending": []}

    _add_product(customer["tenant_id"], "Combo fiesta", "incluye tusi rosado")
    [row] = client.get("/api/v1/platform/tenants", headers=admin["headers"], params={"q": customer["email"]}).json()
    assert row["attention"][0]["key"] == "content_risk"
    assert row["attention"][0]["priority"] == 1
    assert row["attention"][0]["detail"] == "tusi"
    detail = client.get(base, headers=admin["headers"]).json()
    assert detail["content_review"]["findings"] == [{"term": "tusi", "where": "Producto «Combo fiesta»"}]

    res = client.patch(base, headers=admin["headers"], json={"content_reviewed": True})
    assert res.status_code == 200, res.text
    assert res.json()["tenant"]["content_review"]["pending"] == []
    assert all(r["key"] != "content_risk" for r in res.json()["tenant"]["attention"])

    res = client.patch(base, headers=admin["headers"], json={"overrides": {"note": "revisado por teléfono"}})
    assert res.json()["tenant"]["content_review"]["pending"] == []

    _add_product(customer["tenant_id"], "Cytotec")
    detail = client.get(base, headers=admin["headers"]).json()
    assert detail["content_review"]["pending"] == ["cytotec"]
    history = client.get(f"{base}/history", headers=admin["headers"]).json()
    assert any(h["action"] == "platform.content_reviewed" for h in history)

    assert client.patch(base, headers=customer["headers"], json={"content_reviewed": True}).status_code == 404

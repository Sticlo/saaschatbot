from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.application.billing.tenant_profile_service import (
    answers_from_profile,
    apply_business_answers,
    get_or_create_tenant_profile,
)
from app.domain.entities import TenantProfile
from app.infrastructure.persistence.database import SessionLocal
from tests.test_phase1 import _register


def test_business_profile_get_creates_profile(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    res = client.get("/api/v1/outbound/business-profile", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "business_name" in data


def test_business_profile_save_persists(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    payload = {
        "industry": "Lavandería industrial",
        "products_services": "Lavado de hoteles",
        "tone": "Cercano",
    }
    put = client.put("/api/v1/outbound/business-profile", json=payload, headers=headers)
    assert put.status_code == 200
    saved = put.json()
    assert saved["industry"] == payload["industry"]
    assert saved["products_services"] == payload["products_services"]

    get = client.get("/api/v1/outbound/business-profile", headers=headers)
    assert get.status_code == 200
    again = get.json()
    assert again["industry"] == payload["industry"]
    assert again["products_services"] == payload["products_services"]


def test_apply_business_answers_columns():
    profile = TenantProfile(tenant_id=uuid.uuid4(), onboarding_answers={})
    apply_business_answers(profile, {"industry": "Café", "tone": "Formal"})
    assert profile.industry == "Café"
    assert profile.tone == "Formal"
    assert answers_from_profile(profile)["industry"] == "Café"


def test_get_or_create_tenant_profile(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    me = client.get("/api/v1/tenants/me", headers=headers).json()
    tenant_id = uuid.UUID(me["id"])
    with SessionLocal() as db:
        profile = get_or_create_tenant_profile(db, tenant_id)
        assert profile.tenant_id == tenant_id
        again = get_or_create_tenant_profile(db, tenant_id)
        assert again.id == profile.id

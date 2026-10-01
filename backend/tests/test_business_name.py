from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import requires_db
from tests.test_phase1 import _register


def test_placeholder_business_name_helpers():
    from app.application.billing.tenant_service import (
        is_placeholder_business_name,
        placeholder_business_name,
    )

    assert placeholder_business_name("Juan Aguilar") == "Negocio de Juan"
    assert placeholder_business_name(None) == "Negocio de Omitel"
    assert is_placeholder_business_name("Negocio de Juan") is True
    assert is_placeholder_business_name("") is True
    assert is_placeholder_business_name("Motel Luna") is False


@requires_db
def test_owner_renames_placeholder_business(client: TestClient):
    from app.domain.entities import Tenant, TenantProfile
    from app.infrastructure.persistence.database import SessionLocal

    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    tenant_id = uuid.UUID(auth["tenant_id"])
    with SessionLocal() as db:
        db.query(Tenant).filter(Tenant.id == tenant_id).update({"business_name": "Negocio de Juan"})
        db.commit()

    r = client.get("/api/v1/outbound/business-profile", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["business_name_is_placeholder"] is True

    r = client.put(
        "/api/v1/outbound/business-profile",
        json={"business_name": "  Hotel Casa Andina ", "industry": "Hotel"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["business_name"] == "Hotel Casa Andina"
    assert r.json()["business_name_is_placeholder"] is False

    with SessionLocal() as db:
        assert db.get(Tenant, tenant_id).business_name == "Hotel Casa Andina"
        profile = db.query(TenantProfile).filter(TenantProfile.tenant_id == tenant_id).one()
        assert "Hotel Casa Andina" in (profile.ai_system_prompt or "")

    r = client.put("/api/v1/outbound/business-profile", json={"business_name": " "}, headers=headers)
    assert r.status_code == 422

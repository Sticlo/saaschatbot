from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import DEFAULT_PLAN_ID, Plan
from app.services.plan_service import ensure_default_plan

DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://saaschatbot:localdev123@localhost:5432/saaschatbot",
    ),
)


def _db_available() -> bool:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="PostgreSQL no disponible para tests de integración",
)


@pytest.fixture()
def client():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)

    with TestingSessionLocal() as db:
        ensure_default_plan(db)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    engine.dispose()


def _register(client: TestClient, suffix=None) -> dict:
    tag = suffix or uuid.uuid4().hex[:8]
    payload = {
        "business_name": f"Negocio Test {tag}",
        "owner_name": "Owner Test",
        "email": f"owner-{tag}@test.com",
        "password": "password123",
    }
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_list_public_plans(client: TestClient):
    response = client.get("/api/v1/plans")
    assert response.status_code == 200
    plans = response.json()
    assert len(plans) >= 1
    pro = next(p for p in plans if p["slug"] == "pro")
    assert pro["price_cop"] == 80_000
    assert pro["trial_bait_limit"] == 10
    assert pro["daily_bait_limit"] == 100


def test_register_login_and_subscription(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["role"] == "owner"

    sub = client.get("/api/v1/subscriptions/me", headers=headers)
    assert sub.status_code == 200
    body = sub.json()
    assert body["is_trial"] is True
    assert body["trial_bait_remaining"] == 10
    assert body["plan"]["slug"] == "pro"
    assert body["needs_payment"] is False


def test_change_password(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    email = auth["email"]

    bad = client.patch(
        "/api/v1/auth/me/password",
        headers=headers,
        json={"current_password": "wrong", "new_password": "newpassword123"},
    )
    assert bad.status_code == 400

    ok = client.patch(
        "/api/v1/auth/me/password",
        headers=headers,
        json={"current_password": "password123", "new_password": "newpassword123"},
    )
    assert ok.status_code == 204

    login_old = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "password123"},
    )
    assert login_old.status_code == 401

    login_new = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "newpassword123"},
    )
    assert login_new.status_code == 200


def test_audit_logs_owner_only(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    logs = client.get("/api/v1/audit-logs", headers=headers)
    assert logs.status_code == 200
    actions = [row["action"] for row in logs.json()]
    assert "tenant.registered" in actions

"""Evidencia de autorización (Ley 1581 de 2012): ninguna cuenta nueva se crea sin
aceptar Términos y Política de Datos, y cada alta deja un registro auditable."""
from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.application.auth import magic_link_service
from app.application.auth.legal_consent_service import PRIVACY_VERSION, TERMS_VERSION
from app.domain.entities import LegalConsent, User
from tests.conftest import requires_db

pytestmark = requires_db


def _consents_for(email: str) -> list[LegalConsent]:
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        rows = db.query(LegalConsent).filter(LegalConsent.email == email).all()
        for row in rows:
            db.expunge(row)
        return rows


def _user_exists(email: str) -> bool:
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        return db.query(User.id).filter(User.email == email).first() is not None


def _signup_payload(email: str, **extra) -> dict:
    return {
        "business_name": "Negocio Legal",
        "owner_name": "Dueña Legal",
        "email": email,
        "password": "password123",
        **extra,
    }


@pytest.fixture()
def captured_links(monkeypatch):
    urls: list[str] = []

    def fake_send(*, to_email: str, url: str, signup: bool):
        urls.append(url)
        return None

    monkeypatch.setattr(magic_link_service, "send_magic_link_email", fake_send)
    return urls


def _token_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["token"][0]


def test_register_without_acceptance_is_rejected(client: TestClient):
    email = f"legal-no-{uuid.uuid4().hex[:8]}@test.com"
    res = client.post("/api/v1/auth/register", json=_signup_payload(email))
    assert res.status_code == 400
    assert "Términos" in res.json()["detail"]
    assert not _user_exists(email)
    assert _consents_for(email) == []


def test_register_with_acceptance_stores_evidence(client: TestClient):
    email = f"legal-ok-{uuid.uuid4().hex[:8]}@test.com"
    res = client.post(
        "/api/v1/auth/register",
        json=_signup_payload(email, accept_legal=True, accept_marketing=True),
        headers={"User-Agent": "pytest-legal/1.0"},
    )
    assert res.status_code == 201, res.text

    [consent] = _consents_for(email)
    assert consent.method == "registro_contrasena"
    assert consent.terms_version == TERMS_VERSION
    assert consent.privacy_version == PRIVACY_VERSION
    assert consent.marketing_opt_in is True
    assert consent.user_agent == "pytest-legal/1.0"
    assert consent.ip_address
    assert consent.tenant_id is not None and consent.user_id is not None
    assert consent.created_at is not None


def test_marketing_is_opt_in_by_default(client: TestClient):
    email = f"legal-mk-{uuid.uuid4().hex[:8]}@test.com"
    res = client.post("/api/v1/auth/register", json=_signup_payload(email, accept_legal=True))
    assert res.status_code == 201, res.text
    [consent] = _consents_for(email)
    assert consent.marketing_opt_in is False


def test_magic_link_signup_requires_acceptance(client: TestClient, captured_links):
    email = f"legal-ml-{uuid.uuid4().hex[:8]}@test.com"
    res = client.post(
        "/api/v1/auth/magic-link",
        json={"email": email, "business_name": "Negocio ML", "owner_name": "Dueño ML"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["sent"] is False
    assert data["needs_signup"] is True
    assert "Términos" in data["message"]
    assert captured_links == []


def test_magic_link_signup_records_consent_on_first_open(client: TestClient, captured_links):
    email = f"legal-ml-{uuid.uuid4().hex[:8]}@test.com"
    res = client.post(
        "/api/v1/auth/magic-link",
        json={
            "email": email,
            "business_name": "Negocio ML",
            "owner_name": "Dueño ML",
            "accept_legal": True,
            "accept_marketing": True,
        },
        headers={"User-Agent": "pytest-magic/1.0"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["sent"] is True
    # Aceptar no crea la cuenta: eso pasa al abrir el enlace.
    assert not _user_exists(email)
    assert _consents_for(email) == []

    verify = client.post("/api/v1/auth/magic-link/verify", json={"token": _token_from(captured_links[-1])})
    assert verify.status_code == 200, verify.text

    [consent] = _consents_for(email)
    assert consent.method == "registro_enlace_correo"
    assert consent.marketing_opt_in is True
    assert consent.user_agent == "pytest-magic/1.0"
    assert consent.terms_version == TERMS_VERSION


def test_magic_link_without_consent_payload_cannot_create_account(client: TestClient, captured_links):
    email = f"legal-old-{uuid.uuid4().hex[:8]}@test.com"
    # Enlace emitido antes de exigir la aceptación (sin "consent" en el payload).
    token, _ = magic_link_service.create_magic_link(
        email=email, kind="register", business_name="Negocio Viejo", owner_name="Dueño Viejo"
    )
    verify = client.post("/api/v1/auth/magic-link/verify", json={"token": token})
    assert verify.status_code == 400
    assert not _user_exists(email)


def test_magic_link_login_does_not_duplicate_consent(client: TestClient, captured_links):
    email = f"legal-login-{uuid.uuid4().hex[:8]}@test.com"
    assert client.post("/api/v1/auth/register", json=_signup_payload(email, accept_legal=True)).status_code == 201

    res = client.post("/api/v1/auth/magic-link", json={"email": email})
    assert res.json()["sent"] is True
    verify = client.post("/api/v1/auth/magic-link/verify", json={"token": _token_from(captured_links[-1])})
    assert verify.status_code == 200, verify.text
    assert len(_consents_for(email)) == 1


def test_oauth_signup_records_consent_once(client: TestClient):
    from app.application.auth.oauth_service import OAuthProfile, resolve_oauth_user
    from app.infrastructure.persistence.database import SessionLocal

    email = f"legal-oauth-{uuid.uuid4().hex[:8]}@test.com"
    profile = OAuthProfile(provider="google", subject=uuid.uuid4().hex, email=email, full_name="Ana OAuth")

    with SessionLocal() as db:
        resolve_oauth_user(db, profile, ip_address="203.0.113.7")
        db.commit()
    with SessionLocal() as db:
        resolve_oauth_user(db, profile, ip_address="203.0.113.7")
        db.commit()

    [consent] = _consents_for(email)
    assert consent.method == "registro_google"
    assert consent.ip_address == "203.0.113.7"
    assert consent.terms_version == TERMS_VERSION

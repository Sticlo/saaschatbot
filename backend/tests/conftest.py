from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.infrastructure.persistence.database import Base, get_db
from app.presentation.main import app
from app.application.billing.plan_service import ensure_default_plan

DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://saaschatbot:localdev123@localhost:5432/saaschatbot",
    ),
)


def db_available() -> bool:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not db_available(),
    reason="PostgreSQL no disponible para tests de integración",
)


@pytest.fixture()
def wa_offline(monkeypatch):
    """Aísla el procesamiento de webhooks de todo lo externo: Evolution (HTTP y su
    Postgres), la IA y el sync en segundo plano. Devuelve lo que se habría disparado."""
    from app.config import settings
    from app.application.ai import ai_queue_service
    from app.application.chatwoot import chatwoot_service
    from app.application.sync import sync_scheduler
    from app.application.whatsapp import whatsapp_gateway
    from app.infrastructure.evolution.evolution_client import evolution_client

    calls: dict[str, list] = {"ai_jobs": [], "sync_after_connect": []}
    monkeypatch.setattr(settings, "evolution_database_url", "")
    monkeypatch.setattr(chatwoot_service, "chatwoot_sync_mode", lambda: False)
    monkeypatch.setattr(whatsapp_gateway, "uses_waha", lambda: False)
    monkeypatch.setattr(
        ai_queue_service,
        "flush_pending_ai_replies",
        lambda jobs: calls["ai_jobs"].extend(jobs),
    )
    monkeypatch.setattr(
        sync_scheduler,
        "ensure_whatsapp_sync_after_connect",
        lambda tenant_id, force=False: calls["sync_after_connect"].append(tenant_id) or True,
    )
    monkeypatch.setattr(evolution_client, "set_settings", lambda *a, **kw: {})
    return calls


def make_wa_tenant(db, *, label: str = "WA", connected: bool = True, owner_phone: str = "573150000000"):
    """Tenant con una vinculación WhatsApp activa (lo que deja un QR escaneado)."""
    import uuid
    from datetime import datetime, timezone

    from app.domain.entities import Tenant, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus

    status = WhatsAppStatus.CONNECTED.value if connected else WhatsAppStatus.DISCONNECTED.value
    tenant = Tenant(
        business_name=f"{label} Test",
        slug=f"{label.lower()}-{uuid.uuid4().hex[:10]}",
        whatsapp_status=status,
    )
    db.add(tenant)
    db.flush()
    session = WhatsAppSession(
        tenant_id=tenant.id,
        instance_name=f"t_{uuid.uuid4().hex[:12]}",
        status=status,
        active_connection_id=uuid.uuid4(),
        connection_started_at=datetime.now(timezone.utc),
        bound_owner_jid=f"{owner_phone}@s.whatsapp.net",
        phone_number=f"+{owner_phone}",
    )
    db.add(session)
    db.commit()
    # Desacoplados ya cargados: así los commits siguientes (expire_on_commit) no los vacían
    # y se pueden leer después de cerrar la sesión.
    db.refresh(tenant)
    db.refresh(session)
    db.expunge(tenant)
    db.expunge(session)
    return tenant, session


def upsert_payload(
    instance: str,
    *,
    msg_id: str,
    text: str,
    phone: str = "573001112233",
    from_me: bool = False,
    push_name: str = "Cliente",
    timestamp: int | None = None,
) -> dict:
    """`messages.upsert` tal como lo emite Evolution v2."""
    data = {
        "key": {"remoteJid": f"{phone}@s.whatsapp.net", "fromMe": from_me, "id": msg_id},
        "pushName": push_name,
        "message": {"conversation": text},
    }
    if timestamp is not None:
        data["messageTimestamp"] = timestamp
    return {"event": "messages.upsert", "instance": instance, "data": data}


def status_payload(instance: str, *, msg_id: str, status: str, phone: str = "573001112233") -> dict:
    """`messages.update` de Evolution v2: el id de WhatsApp viene en `keyId`, sin objeto `key`."""
    return {
        "event": "messages.update",
        "instance": instance,
        "data": {
            "messageId": "evo-internal-row-id",
            "keyId": msg_id,
            "remoteJid": f"{phone}@s.whatsapp.net",
            "fromMe": True,
            "status": status,
        },
    }


def connection_payload(instance: str, state: str, owner_phone: str = "573150000000") -> dict:
    data = {"instance": instance, "state": state, "statusReason": 200 if state == "open" else 428}
    if state == "open":
        data["wuid"] = f"{owner_phone}@s.whatsapp.net"
    return {"event": "connection.update", "instance": instance, "data": data}


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

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import settings

# Los tests crean miles de empresas falsas: nunca deben tocar la base de datos real.
# Esto tiene que correr antes de importar la app, que arma su engine con settings.database_url.
TEST_REDIS_DB = 15


def _test_database_url() -> str:
    explicit = os.getenv("TEST_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    url = make_url(settings.database_url)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


DATABASE_URL = _test_database_url()
if not (make_url(DATABASE_URL).database or "").endswith("_test"):
    raise RuntimeError("TEST_DATABASE_URL debe apuntar a una base cuyo nombre termine en _test")
settings.database_url = DATABASE_URL
settings.redis_url = urlunsplit(urlsplit(settings.redis_url)._replace(path=f"/{TEST_REDIS_DB}"))
# Ni cobros ni consultas reales a Wompi: cada test que lo necesite pone sus propias llaves falsas.
settings.wompi_public_key = ""
settings.wompi_private_key = ""
settings.wompi_integrity_secret = ""
settings.wompi_events_secret = ""
settings.wompi_api_base_url = ""


def _prepare_test_database() -> bool:
    """Crea la base _test si falta y la deja en la última migración."""
    url = make_url(DATABASE_URL)
    try:
        admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
        admin.dispose()

        from pathlib import Path

        from alembic import command
        from alembic.config import Config

        backend_dir = Path(__file__).resolve().parents[1]
        cfg = Config()
        cfg.set_main_option("script_location", str(backend_dir / "alembic"))
        command.upgrade(cfg, "head")
        return True
    except Exception as exc:  # sin Postgres: los tests de integración se saltan
        print(f"[tests] Base de pruebas no disponible: {exc}")
        return False


_DB_READY = _prepare_test_database()

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.application.billing.plan_service import ensure_default_plan  # noqa: E402
from app.infrastructure.persistence.database import Base, get_db  # noqa: E402
from app.presentation.main import app  # noqa: E402


def db_available() -> bool:
    return _DB_READY


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
    """Tenant con una vinculación WhatsApp activa (QR escaneado hace un día)."""
    import uuid
    from datetime import datetime, timedelta, timezone

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
        connection_started_at=datetime.now(timezone.utc) - timedelta(days=1),
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


@pytest.fixture(autouse=True)
def _disable_rate_limits_in_tests(monkeypatch):
    """El límite por IP de 30 auth/min tumba la suite (todos los tests salen de testclient)."""
    from app.config import settings

    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    # Los tests nunca deben mandar alertas reales a Telegram/correo.
    monkeypatch.setattr(settings, "ops_alerts_enabled", False)


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

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from tests.conftest import requires_db

OWNER = "573001112233@s.whatsapp.net"


@requires_db
def test_binding_repair_never_reuses_old_connection(monkeypatch):
    from app.application.conversations import whatsapp_conversation_service as binding
    from app.domain.entities import Conversation, Tenant, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr("app.config.settings.evolution_database_url", "")
    old_connection_id = uuid.uuid4()

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Fresh Binding",
            slug=f"fresh-binding-{uuid.uuid4().hex[:8]}",
            whatsapp_status=WhatsAppStatus.CONNECTED.value,
        )
        db.add(tenant)
        db.flush()
        session = WhatsAppSession(
            tenant_id=tenant.id,
            instance_name=f"inst_{uuid.uuid4().hex[:8]}",
            status=WhatsAppStatus.CONNECTED.value,
            active_connection_id=None,
            connection_started_at=None,
        )
        db.add(session)
        db.add(
            Conversation(
                tenant_id=tenant.id,
                contact_phone="+573004583560",
                contact_name="Sesión anterior",
                whatsapp_connection_id=old_connection_id,
            )
        )
        db.flush()

        repaired = binding.ensure_whatsapp_binding_ready(
            db,
            tenant=tenant,
            session=session,
        )
        db.flush()

        assert repaired is True
        assert session.active_connection_id is not None
        assert session.active_connection_id != old_connection_id
        assert (
            db.query(Conversation)
            .filter(Conversation.tenant_id == tenant.id)
            .count()
            == 0
        )
        db.rollback()


def test_only_a_different_phone_starts_a_new_binding():
    from app.application.conversations.whatsapp_conversation_service import needs_new_whatsapp_binding

    bound = SimpleNamespace(active_connection_id=uuid.uuid4(), bound_owner_jid=OWNER)
    assert needs_new_whatsapp_binding(bound, mapped_status="connected", owner_jid=OWNER) is False
    assert needs_new_whatsapp_binding(bound, mapped_status="connected", owner_jid="573001112233:7@s.whatsapp.net") is False
    assert needs_new_whatsapp_binding(bound, mapped_status="connected", owner_jid=None) is False
    assert needs_new_whatsapp_binding(bound, mapped_status="connected", owner_jid="573009998877@s.whatsapp.net") is True
    assert needs_new_whatsapp_binding(bound, mapped_status="disconnected", owner_jid="573009998877@s.whatsapp.net") is False

    unbound = SimpleNamespace(active_connection_id=None, bound_owner_jid=None)
    assert needs_new_whatsapp_binding(unbound, mapped_status="connected", owner_jid=OWNER) is True


def _linked_tenant(db):
    from app.domain.entities import Conversation, Tenant, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus

    connection_id = uuid.uuid4()
    tenant = Tenant(
        business_name="Motel Luna",
        slug=f"luna-{uuid.uuid4().hex[:8]}",
        whatsapp_status=WhatsAppStatus.CONNECTED.value,
    )
    db.add(tenant)
    db.flush()
    session = WhatsAppSession(
        tenant_id=tenant.id,
        instance_name=f"inst_{uuid.uuid4().hex[:8]}",
        status=WhatsAppStatus.CONNECTED.value,
        active_connection_id=connection_id,
        connection_started_at=datetime.now(timezone.utc),
        bound_owner_jid=OWNER,
        phone_number="+573001112233",
    )
    db.add(session)
    db.add(
        Conversation(
            tenant_id=tenant.id,
            contact_phone="+573004583560",
            contact_name="Cliente",
            whatsapp_connection_id=connection_id,
        )
    )
    db.flush()
    return tenant, session, connection_id


def _chat_count(db, tenant) -> int:
    from app.domain.entities import Conversation

    return db.query(Conversation).filter(Conversation.tenant_id == tenant.id).count()


@requires_db
def test_drop_and_reconnect_keeps_chats(monkeypatch):
    from app.application.conversations.whatsapp_conversation_service import conversations_visible_for_tenant
    from app.application.messaging.message_service import handle_connection_update
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr("app.config.settings.evolution_database_url", "")
    monkeypatch.setattr("app.infrastructure.evolution.evolution_client.evolution_client.set_settings", lambda *_a, **_kw: None)

    with SessionLocal() as db:
        tenant, session, connection_id = _linked_tenant(db)

        handle_connection_update(db, tenant=tenant, session=session, data={"state": "close"})
        assert tenant.whatsapp_status == "disconnected"
        assert session.active_connection_id == connection_id
        assert _chat_count(db, tenant) == 1
        assert conversations_visible_for_tenant(db, tenant=tenant, session=session) is True

        handle_connection_update(db, tenant=tenant, session=session, data={"state": "open", "wuid": OWNER})
        assert tenant.whatsapp_status == "connected"
        assert session.active_connection_id == connection_id
        assert _chat_count(db, tenant) == 1

        handle_connection_update(
            db, tenant=tenant, session=session, data={"state": "open", "wuid": "573009998877@s.whatsapp.net"}
        )
        assert session.active_connection_id != connection_id
        assert _chat_count(db, tenant) == 0
        db.rollback()


@requires_db
def test_status_poll_never_purges_on_drop(monkeypatch):
    from app.application.whatsapp import whatsapp_service
    from app.application.whatsapp.whatsapp_gateway import WhatsAppGatewayError
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr("app.config.settings.evolution_database_url", "")
    monkeypatch.setattr(whatsapp_service, "gateway_fetch_instance", lambda _name: None)

    def evolution_down(_name):
        raise WhatsAppGatewayError("Evolution caído")

    with SessionLocal() as db:
        tenant, session, connection_id = _linked_tenant(db)

        monkeypatch.setattr(whatsapp_service, "gateway_connection_state", evolution_down)
        whatsapp_service.refresh_session_status(db, tenant, session)
        assert tenant.whatsapp_status == "disconnected"
        assert session.bound_owner_jid == OWNER
        assert _chat_count(db, tenant) == 1

        monkeypatch.setattr(
            whatsapp_service, "gateway_connection_state", lambda _name: {"instance": {"state": "close"}}
        )
        whatsapp_service.refresh_session_status(db, tenant, session)
        assert _chat_count(db, tenant) == 1

        monkeypatch.setattr(
            whatsapp_service,
            "gateway_connection_state",
            lambda _name: {"instance": {"state": "open", "owner": OWNER}},
        )
        whatsapp_service.refresh_session_status(db, tenant, session)
        assert tenant.whatsapp_status == "connected"
        assert session.active_connection_id == connection_id
        assert _chat_count(db, tenant) == 1
        db.rollback()


@requires_db
def test_watchdog_reconnects_and_backs_off_when_qr_needed(monkeypatch):
    from app.application.whatsapp import whatsapp_reconnect_service as watchdog
    from app.infrastructure.cache.redis_client import cache_get
    from app.infrastructure.persistence.database import SessionLocal

    calls: list[str] = []
    refreshed: list[str] = []
    monkeypatch.setattr(watchdog, "connection_state", lambda _name: {"instance": {"state": "close"}})
    monkeypatch.setattr(
        watchdog, "connect_instance", lambda name: calls.append(name) or {"base64": "data:image/png;base64,QR"}
    )
    monkeypatch.setattr(
        "app.application.whatsapp.whatsapp_service.refresh_session_status",
        lambda db, tenant, session: refreshed.append(session.instance_name),
    )

    with SessionLocal() as db:
        tenant, session, _ = _linked_tenant(db)
        tenant.whatsapp_status = "disconnected"

        assert watchdog.try_reconnect(db, tenant, session) is False
        assert calls == [session.instance_name]
        assert cache_get(watchdog._backoff_key(tenant))

        assert watchdog.try_reconnect(db, tenant, session) is False
        assert calls == [session.instance_name]

        from app.infrastructure.cache.redis_client import cache_delete

        cache_delete(watchdog._backoff_key(tenant))
        monkeypatch.setattr(watchdog, "connection_state", lambda _name: {"instance": {"state": "open"}})
        monkeypatch.setattr(db, "commit", lambda: None)
        watchdog.try_reconnect(db, tenant, session)
        assert refreshed == [session.instance_name]
        db.rollback()

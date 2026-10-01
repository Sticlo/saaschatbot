from __future__ import annotations

import uuid

from tests.conftest import requires_db


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
        db.commit()

        repaired = binding.ensure_whatsapp_binding_ready(
            db,
            tenant=tenant,
            session=session,
        )
        db.commit()

        assert repaired is True
        assert session.active_connection_id is not None
        assert session.active_connection_id != old_connection_id
        assert (
            db.query(Conversation)
            .filter(Conversation.tenant_id == tenant.id)
            .count()
            == 0
        )

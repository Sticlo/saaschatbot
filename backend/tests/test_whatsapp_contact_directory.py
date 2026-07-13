from __future__ import annotations

import uuid

from tests.conftest import requires_db


@requires_db
def test_directory_only_exposes_direct_or_current_verified_contacts(monkeypatch):
    from app.application.outbound import whatsapp_contact_directory_service as service
    from app.domain.entities import Tenant, WhatsAppContactLink, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus
    from app.infrastructure.persistence.database import SessionLocal

    connection_id = uuid.uuid4()
    old_connection_id = uuid.uuid4()

    monkeypatch.setattr(service.settings, "evolution_database_url", "")
    monkeypatch.setattr(
        service.evolution_client,
        "find_contacts",
        lambda _instance: [
            {
                "remoteJid": "573004583560@s.whatsapp.net",
                "name": "Esteban",
            },
            {
                "remoteJid": "71021579251813@lid",
                "name": "Primito",
            },
            {
                "remoteJid": "236429376532542@lid",
                "name": "Ambiguo",
            },
            {
                "remoteJid": "573017453703@s.whatsapp.net",
                "name": "Dueño",
            },
        ],
    )

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Safe Directory",
            slug=f"safe-dir-{uuid.uuid4().hex[:8]}",
            whatsapp_status=WhatsAppStatus.CONNECTED.value,
        )
        db.add(tenant)
        db.flush()
        session = WhatsAppSession(
            tenant_id=tenant.id,
            instance_name=f"inst_{uuid.uuid4().hex[:8]}",
            status=WhatsAppStatus.CONNECTED.value,
            phone_number="+573017453703",
            active_connection_id=connection_id,
        )
        db.add(session)
        db.add_all(
            [
                WhatsAppContactLink(
                    tenant_id=tenant.id,
                    whatsapp_connection_id=connection_id,
                    lid_jid="71021579251813@lid",
                    phone_e164="+573219469201",
                ),
                WhatsAppContactLink(
                    tenant_id=tenant.id,
                    whatsapp_connection_id=old_connection_id,
                    lid_jid="236429376532542@lid",
                    phone_e164="+573105555555",
                ),
            ]
        )
        db.commit()

        result = service.list_safe_whatsapp_contacts(db, session=session)
        by_phone = {row["phone_e164"]: row for row in result["contacts"]}

        assert set(by_phone) == {"+573004583560", "+573219469201"}
        assert by_phone["+573004583560"]["name"] == "Esteban"
        assert by_phone["+573219469201"]["name"] == "Primito"
        assert result["omitted_ambiguous"] == 1


@requires_db
def test_selected_contact_validation_rejects_unknown_phone(monkeypatch):
    from app.application.outbound import whatsapp_contact_directory_service as service
    from app.domain.entities import Tenant, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr(service.settings, "evolution_database_url", "")
    monkeypatch.setattr(
        service.evolution_client,
        "find_contacts",
        lambda _instance: [
            {"remoteJid": "573004583560@s.whatsapp.net", "name": "Esteban"},
        ],
    )

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Safe Selection",
            slug=f"safe-select-{uuid.uuid4().hex[:8]}",
            whatsapp_status=WhatsAppStatus.CONNECTED.value,
        )
        db.add(tenant)
        db.flush()
        session = WhatsAppSession(
            tenant_id=tenant.id,
            instance_name=f"inst_{uuid.uuid4().hex[:8]}",
            status=WhatsAppStatus.CONNECTED.value,
            active_connection_id=uuid.uuid4(),
        )
        db.add(session)
        db.commit()

        selected, rejected = service.validate_selected_contact_phones(
            db,
            session=session,
            phones=["+573004583560", "+573009999999"],
        )

        assert selected == [{"phone": "+573004583560", "name": "Esteban"}]
        assert rejected == ["+573009999999"]


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

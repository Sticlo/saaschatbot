from __future__ import annotations

import uuid

from app.shared.core.phone import phone_match_tail
from app.domain.entities import Conversation
from app.application.messaging.message_service import find_conversation_for_contact, save_outbound_from_phone
from tests.conftest import requires_db


def test_phone_match_tail_ignores_country_prefix():
    assert phone_match_tail("+573001234567", "3001234567") is True
    assert phone_match_tail("573001234567", "+573001234567") is True
    assert phone_match_tail("+573001234567", "+573009999999") is False


@requires_db
def test_find_conversation_by_phone_tail():
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Tenant

    connection_id = uuid.uuid4()

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Tail Test",
            slug=f"tail-{uuid.uuid4().hex[:8]}",
        )
        db.add(tenant)
        db.flush()

        conv = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573001234567",
            contact_name="Juan",
            whatsapp_connection_id=connection_id,
        )
        db.add(conv)
        db.commit()

        found = find_conversation_for_contact(
            db,
            tenant_id=tenant.id,
            whatsapp_connection_id=connection_id,
            contact_phone="3001234567",
        )
        assert found is not None
        assert found.id == conv.id


@requires_db
def test_outbound_phone_echo_does_not_reuse_unverified_lid_chat():
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Message, Tenant
    from app.domain.entities.enums import MessageDirection, MessageSource, MessageStatus
    from app.application.messaging.message_service import _save_message

    connection_id = uuid.uuid4()

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Lid Echo",
            slug=f"lid-echo-{uuid.uuid4().hex[:8]}",
        )
        db.add(tenant)
        db.flush()

        girlfriend = Conversation(
            tenant_id=tenant.id,
            contact_phone="lid:236429376532542",
            contact_name="Mi Novia",
            contact_jid="236429376532542@lid",
            whatsapp_connection_id=connection_id,
        )
        db.add(girlfriend)
        db.flush()

        _save_message(
            db,
            tenant=tenant,
            conversation=girlfriend,
            direction=MessageDirection.OUT.value,
            source=MessageSource.AGENT.value,
            body="Te amo",
            status=MessageStatus.SENT.value,
            evolution_message_id="",
            increment_unread=False,
        )
        db.commit()

        echoed = save_outbound_from_phone(
            db,
            tenant=tenant,
            evolution_message_id="",
            remote_jid="573219469201@s.whatsapp.net",
            body="Te amo",
            lid_jid="",
            whatsapp_connection_id=connection_id,
        )
        assert echoed is not None
        assert echoed.conversation_id != girlfriend.id

        count = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant.id,
                Conversation.whatsapp_connection_id == connection_id,
            )
            .count()
        )
        assert count == 2


def test_pick_merge_primary_prefers_lid_over_owner_name():
    from app.application.conversations.whatsapp_conversation_service import pick_merge_primary

    connection_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    lid_chat = Conversation(
        tenant_id=tenant_id,
        contact_phone="lid:236429376532542",
        contact_name="ADRIANA",
        contact_jid="236429376532542@lid",
        whatsapp_connection_id=connection_id,
    )
    phone_chat = Conversation(
        tenant_id=tenant_id,
        contact_phone="+573219469201",
        contact_name="Juan Aguilar",
        whatsapp_connection_id=connection_id,
    )

    primary, secondary = pick_merge_primary(
        lid_chat,
        phone_chat,
        owner_names={"Juan Aguilar"},
    )
    # Con un merge ya verificado, conservar la identidad con teléfono apto
    # para envío es más seguro que el placeholder @lid.
    assert primary is phone_chat
    assert secondary is lid_chat


@requires_db
def test_outbound_from_phone_finds_lid_chat_via_remote_jid_alt():
    """Mensaje desde celular con remoteJidAlt=@lid debe ir al chat @lid existente."""
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Tenant

    connection_id = uuid.uuid4()

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Cell Lid Alt",
            slug=f"cell-alt-{uuid.uuid4().hex[:8]}",
        )
        db.add(tenant)
        db.flush()

        novia = Conversation(
            tenant_id=tenant.id,
            contact_phone="lid:236429376532542",
            contact_name="ADRIANA",
            contact_jid="236429376532542@lid",
            whatsapp_connection_id=connection_id,
        )
        db.add(novia)
        db.commit()

        echoed = save_outbound_from_phone(
            db,
            tenant=tenant,
            evolution_message_id="CELL_E_1",
            remote_jid="573219469201@s.whatsapp.net",
            body="e",
            message_key={
                "remoteJid": "573219469201@s.whatsapp.net",
                "remoteJidAlt": "236429376532542@lid",
                "fromMe": True,
                "id": "CELL_E_1",
            },
            lid_jid="236429376532542@lid",
            whatsapp_connection_id=connection_id,
        )
        assert echoed is not None
        assert echoed.conversation_id == novia.id

        count = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant.id,
                Conversation.whatsapp_connection_id == connection_id,
            )
            .count()
        )
        assert count == 1


@requires_db
def test_outbound_lid_echo_does_not_guess_recent_chat_without_verified_key():
    """Un @lid sin pareja verificada nunca se asigna por similitud de texto."""
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Tenant
    from app.domain.entities.enums import MessageDirection, MessageSource, MessageStatus
    from app.application.messaging.message_service import _save_message

    connection_id = uuid.uuid4()

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Lid Echo Id",
            slug=f"lid-id-{uuid.uuid4().hex[:8]}",
        )
        db.add(tenant)
        db.flush()

        novia = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573219469201",
            contact_name="+573219469201",
            contact_jid="",
            whatsapp_connection_id=connection_id,
        )
        db.add(novia)
        db.flush()

        _save_message(
            db,
            tenant=tenant,
            conversation=novia,
            direction=MessageDirection.OUT.value,
            source=MessageSource.AGENT.value,
            body="te quiero",
            status=MessageStatus.SENT.value,
            evolution_message_id="",
            increment_unread=False,
        )
        db.commit()

        echoed = save_outbound_from_phone(
            db,
            tenant=tenant,
            evolution_message_id="WA_ECHO_123",
            remote_jid="236429376532542@lid",
            body="te quiero",
            lid_jid="236429376532542@lid",
            whatsapp_connection_id=connection_id,
        )
        assert echoed is not None
        assert echoed.conversation_id != novia.id
        db.commit()

        count = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant.id,
                Conversation.whatsapp_connection_id == connection_id,
            )
            .count()
        )
        assert count == 2

        db.refresh(novia)
        assert novia.contact_jid == ""
        assert novia.contact_name == "+573219469201"


@requires_db
def test_outbound_webhook_reuses_existing_chat_without_message_id():
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Message, Tenant, WhatsAppSession
    from app.domain.entities.enums import MessageDirection, MessageSource, MessageStatus, WhatsAppStatus
    from app.application.messaging.message_service import _save_message

    connection_id = uuid.uuid4()

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Dup Test",
            slug=f"dup-{uuid.uuid4().hex[:8]}",
        )
        db.add(tenant)
        db.flush()

        session = WhatsAppSession(
            tenant_id=tenant.id,
            instance_name=f"inst_{uuid.uuid4().hex[:8]}",
            status=WhatsAppStatus.CONNECTED.value,
        )
        db.add(session)

        conv = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573001234567",
            contact_name="Lead",
            whatsapp_connection_id=connection_id,
        )
        db.add(conv)
        db.flush()

        _save_message(
            db,
            tenant=tenant,
            conversation=conv,
            direction=MessageDirection.OUT.value,
            source=MessageSource.AGENT.value,
            body="Hola desde panel",
            status=MessageStatus.SENT.value,
            evolution_message_id="",
            increment_unread=False,
        )
        db.commit()

        echoed = save_outbound_from_phone(
            db,
            tenant=tenant,
            evolution_message_id="",
            remote_jid="573001234567@s.whatsapp.net",
            body="Hola desde panel",
            whatsapp_connection_id=connection_id,
        )
        assert echoed is not None
        assert echoed.conversation_id == conv.id

        count = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant.id,
                Conversation.whatsapp_connection_id == connection_id,
            )
            .count()
        )
        assert count == 1

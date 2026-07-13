from __future__ import annotations

import uuid

from app.shared.core.phone import (
    is_untrusted_contact_phone,
    pick_trusted_phone,
    collect_phone_candidates,
)
from app.application.conversations.whatsapp_conversation_service import (
    repair_duplicate_conversations,
)
from app.domain.entities import Conversation, Message, Tenant
from app.domain.entities.enums import MessageDirection
from tests.conftest import requires_db


def test_untrusted_pattern_phone():
    assert is_untrusted_contact_phone("+573001234567") is True
    assert is_untrusted_contact_phone("+573004583560") is False


def test_pick_trusted_phone_prefers_real_number():
    candidates = collect_phone_candidates(
        "573001234567@s.whatsapp.net",
        key={
            "remoteJid": "573001234567@s.whatsapp.net",
            "remoteJidAlt": "573004583560@s.whatsapp.net",
        },
    )
    assert "+573004583560" in candidates
    assert pick_trusted_phone(candidates) == "+573004583560"


@requires_db
def test_repair_merges_lid_with_fake_phone_and_real_phone_chat():
    from app.domain.entities import WhatsAppContactLink
    from app.infrastructure.persistence.database import SessionLocal

    connection_id = uuid.uuid4()
    lid_jid = "71021579251813@lid"

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Untrusted Merge",
            slug=f"untrusted-{uuid.uuid4().hex[:8]}",
        )
        db.add(tenant)
        db.flush()

        lid_chat = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573001234567",
            contact_name="Julián Alarcón",
            contact_jid=lid_jid,
            whatsapp_connection_id=connection_id,
        )
        phone_chat = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573004583560",
            contact_name="+573004583560",
            whatsapp_connection_id=connection_id,
        )
        db.add(lid_chat)
        db.add(phone_chat)
        db.flush()
        db.add(
            WhatsAppContactLink(
                tenant_id=tenant.id,
                whatsapp_connection_id=connection_id,
                lid_jid=lid_jid,
                phone_e164="+573004583560",
            )
        )
        db.commit()

        merged = repair_duplicate_conversations(
            db,
            tenant_id=tenant.id,
            connection_id=connection_id,
        )
        db.commit()

        assert merged >= 1
        remaining = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant.id,
                Conversation.whatsapp_connection_id == connection_id,
            )
            .all()
        )
        assert len(remaining) == 1


@requires_db
def test_repair_does_not_merge_different_contacts_with_same_short_outbound():
    from app.infrastructure.persistence.database import SessionLocal

    connection_id = uuid.uuid4()

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="No Short Body Merge",
            slug=f"short-body-{uuid.uuid4().hex[:8]}",
        )
        db.add(tenant)
        db.flush()

        tia_diana = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573004678377",
            contact_name="Tía Diana",
            whatsapp_connection_id=connection_id,
        )
        diana = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573208725610",
            contact_name="Diana 🐶🐱",
            contact_jid="131735270514761@lid",
            whatsapp_connection_id=connection_id,
        )
        db.add(tia_diana)
        db.add(diana)
        db.flush()
        for conv in (tia_diana, diana):
            db.add(
                Message(
                    tenant_id=tenant.id,
                    conversation_id=conv.id,
                    direction=MessageDirection.OUT.value,
                    source="agent",
                    body="e",
                    status="sent",
                )
            )
        db.commit()

        merged = repair_duplicate_conversations(
            db,
            tenant_id=tenant.id,
            connection_id=connection_id,
        )
        db.commit()

        assert merged == 0
        remaining = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant.id,
                Conversation.whatsapp_connection_id == connection_id,
            )
            .all()
        )
        assert len(remaining) == 2
@requires_db
def test_repair_ignores_link_from_previous_connection():
    """Una vinculación QR vieja nunca puede fusionar contactos de la actual."""
    from app.domain.entities import WhatsAppContactLink
    from app.infrastructure.persistence.database import SessionLocal

    connection_id = uuid.uuid4()
    old_connection_id = uuid.uuid4()
    lid_jid = "71021579251813@lid"

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Cross Connection Merge",
            slug=f"cross-conn-{uuid.uuid4().hex[:8]}",
        )
        db.add(tenant)
        db.flush()

        phone_chat = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573004583560",
            contact_name="Primito",
            whatsapp_connection_id=connection_id,
        )
        lid_chat = Conversation(
            tenant_id=tenant.id,
            contact_phone="lid:71021579251813",
            contact_name="Julián Alarcón",
            contact_jid=lid_jid,
            whatsapp_connection_id=connection_id,
        )
        db.add(phone_chat)
        db.add(lid_chat)
        db.flush()
        db.add(
            WhatsAppContactLink(
                tenant_id=tenant.id,
                whatsapp_connection_id=old_connection_id,
                lid_jid=lid_jid,
                phone_e164="+573004583560",
            )
        )
        for i in range(3):
            db.add(
                Message(
                    tenant_id=tenant.id,
                    conversation_id=phone_chat.id,
                    direction=MessageDirection.IN.value,
                    source="contact",
                    body=f"hola-{i}",
                    status="received",
                )
            )
        db.add(
            Message(
                tenant_id=tenant.id,
                conversation_id=lid_chat.id,
                direction=MessageDirection.OUT.value,
                source="agent",
                body="J",
                status="sent",
            )
        )
        db.commit()

        merged = repair_duplicate_conversations(
            db,
            tenant_id=tenant.id,
            connection_id=connection_id,
        )
        db.commit()

        assert merged == 0
        remaining = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant.id,
                Conversation.whatsapp_connection_id == connection_id,
            )
            .all()
        )
        assert len(remaining) == 2


@requires_db
def test_repair_merges_named_lid_chat_with_phone_only_chat():
    """Katherin Roballo +573… — mismo contacto, dos chats."""
    from app.domain.entities import WhatsAppContactLink
    from app.infrastructure.persistence.database import SessionLocal

    connection_id = uuid.uuid4()
    lid_jid = "71021579251813@lid"

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Named Phone Merge",
            slug=f"named-phone-{uuid.uuid4().hex[:8]}",
        )
        db.add(tenant)
        db.flush()

        named_chat = Conversation(
            tenant_id=tenant.id,
            contact_phone="lid:71021579251813",
            contact_name="Katherin Roballo",
            contact_jid=lid_jid,
            whatsapp_connection_id=connection_id,
        )
        phone_chat = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573219469201",
            contact_name="+573219469201",
            whatsapp_connection_id=connection_id,
        )
        db.add(named_chat)
        db.add(phone_chat)
        db.flush()
        db.add(
            WhatsAppContactLink(
                tenant_id=tenant.id,
                whatsapp_connection_id=connection_id,
                lid_jid=lid_jid,
                phone_e164="+573219469201",
            )
        )
        db.commit()

        merged = repair_duplicate_conversations(
            db,
            tenant_id=tenant.id,
            connection_id=connection_id,
        )
        db.commit()

        assert merged >= 1
        remaining = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant.id,
                Conversation.whatsapp_connection_id == connection_id,
            )
            .all()
        )
        assert len(remaining) == 1
        assert remaining[0].contact_phone == "+573219469201"
        assert remaining[0].contact_name == "Katherin Roballo"

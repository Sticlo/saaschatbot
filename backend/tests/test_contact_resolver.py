from __future__ import annotations

import uuid

from app.domain.entities import Conversation
from app.application.conversations.contact_resolver_service import (
    extract_message_identities,
    record_contact_link,
    resolve_canonical_conversation,
)
from app.application.messaging.message_service import save_outbound_from_phone
from tests.conftest import requires_db


def test_extract_message_identities_phone_primary_with_lid_alt():
    phone, lid = extract_message_identities(
        "573219469201@s.whatsapp.net",
        lid_jid="",
        message_key={
            "remoteJid": "573219469201@s.whatsapp.net",
            "remoteJidAlt": "236429376532542@lid",
            "fromMe": True,
        },
    )
    assert phone == "+573219469201"
    assert lid == "236429376532542@lid"


@requires_db
def test_resolve_canonical_picks_phone_without_merging_lid_chat():
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Tenant

    connection_id = uuid.uuid4()

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Resolver Merge",
            slug=f"resolver-{uuid.uuid4().hex[:8]}",
        )
        db.add(tenant)
        db.flush()

        lid_chat = Conversation(
            tenant_id=tenant.id,
            contact_phone="lid:236429376532542",
            contact_name="ADRIANA",
            contact_jid="236429376532542@lid",
            whatsapp_connection_id=connection_id,
        )
        phone_chat = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573219469201",
            contact_name="+573219469201",
            whatsapp_connection_id=connection_id,
        )
        db.add(lid_chat)
        db.add(phone_chat)
        db.commit()

        conv, phone, lid = resolve_canonical_conversation(
            db,
            tenant_id=tenant.id,
            whatsapp_connection_id=connection_id,
            remote_jid="573219469201@s.whatsapp.net",
            lid_jid="236429376532542@lid",
            message_key={
                "remoteJid": "573219469201@s.whatsapp.net",
                "remoteJidAlt": "236429376532542@lid",
                "fromMe": True,
            },
        )
        db.commit()

        assert conv is not None
        assert conv.contact_phone == "+573219469201"
        assert lid == "236429376532542@lid"
        assert phone == "+573219469201"

        remaining = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant.id,
                Conversation.whatsapp_connection_id == connection_id,
            )
            .count()
        )
        # El message key trae teléfono + @lid juntos: es evidencia fuerte y
        # ambos registros se consolidan en una sola persona.
        assert remaining == 1

        record_contact_link(
            db,
            tenant_id=tenant.id,
            whatsapp_connection_id=connection_id,
            lid_jid="236429376532542@lid",
            phone_e164="+573219469201",
            verified=True,
        )
        db.commit()

        echoed = save_outbound_from_phone(
            db,
            tenant=tenant,
            evolution_message_id="RESOLVER_ECHO_1",
            remote_jid="573219469201@s.whatsapp.net",
            body="hola desde cel",
            message_key={
                "remoteJid": "573219469201@s.whatsapp.net",
                "remoteJidAlt": "236429376532542@lid",
                "fromMe": True,
                "id": "RESOLVER_ECHO_1",
            },
            lid_jid="236429376532542@lid",
            whatsapp_connection_id=connection_id,
        )
        db.commit()
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

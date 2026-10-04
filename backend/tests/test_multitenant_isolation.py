"""Aislamiento entre negocios (tenants): nada de un tenant puede verse, deduplicarse
ni modificarse desde otro, aunque compartan cliente, número o id de mensaje."""
from __future__ import annotations

import uuid

from app.application.messaging.message_service import (
    find_conversation_for_contact,
    save_inbound_message,
    update_message_status,
)
from app.application.messaging.webhook_processor import process_evolution_webhook
from app.application.workers.queue_service import is_duplicate_webhook
from app.domain.entities import Conversation, Message
from tests.conftest import make_wa_tenant, requires_db, status_payload, upsert_payload


def _messages(db, tenant_id):
    return db.query(Message).filter(Message.tenant_id == tenant_id).all()


def _conversations(db, tenant_id):
    return db.query(Conversation).filter(Conversation.tenant_id == tenant_id).all()


def test_webhook_dedup_is_scoped_per_tenant():
    """El mismo id de evento en dos tenants no es un duplicado."""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    dedup_id = f"messages.upsert:{uuid.uuid4().hex}"

    assert is_duplicate_webhook(tenant_a, dedup_id) is False
    assert is_duplicate_webhook(tenant_b, dedup_id) is False
    assert is_duplicate_webhook(tenant_a, dedup_id) is True


@requires_db
def test_same_customer_writing_to_two_businesses_gets_two_separate_chats(wa_offline):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        pizzeria, pizzeria_wa = make_wa_tenant(db, label="Pizzeria")
        hotel, hotel_wa = make_wa_tenant(db, label="Hotel")

    process_evolution_webhook(pizzeria.id, upsert_payload(pizzeria_wa.instance_name, msg_id="P-1", text="Una hawaiana"))
    process_evolution_webhook(hotel.id, upsert_payload(hotel_wa.instance_name, msg_id="H-1", text="Habitación doble"))

    with SessionLocal() as db:
        pizzeria_msgs = _messages(db, pizzeria.id)
        hotel_msgs = _messages(db, hotel.id)
        assert [m.body for m in pizzeria_msgs] == ["Una hawaiana"]
        assert [m.body for m in hotel_msgs] == ["Habitación doble"]
        assert len(_conversations(db, pizzeria.id)) == 1
        assert len(_conversations(db, hotel.id)) == 1
        assert pizzeria_msgs[0].conversation_id != hotel_msgs[0].conversation_id


@requires_db
def test_same_whatsapp_message_id_in_two_tenants_is_stored_in_both(wa_offline):
    """La deduplicación por id de WhatsApp no puede cruzar tenants."""
    from app.infrastructure.persistence.database import SessionLocal

    shared_id = f"3EB0{uuid.uuid4().hex[:16].upper()}"
    with SessionLocal() as db:
        tenant_a, wa_a = make_wa_tenant(db, label="A")
        tenant_b, wa_b = make_wa_tenant(db, label="B")

    process_evolution_webhook(tenant_a.id, upsert_payload(wa_a.instance_name, msg_id=shared_id, text="hola A"))
    process_evolution_webhook(tenant_b.id, upsert_payload(wa_b.instance_name, msg_id=shared_id, text="hola B"))

    with SessionLocal() as db:
        assert [m.body for m in _messages(db, tenant_a.id)] == ["hola A"]
        assert [m.body for m in _messages(db, tenant_b.id)] == ["hola B"]


@requires_db
def test_webhook_from_another_tenants_instance_is_rejected(wa_offline):
    """Un payload firmado por la instancia de A que llega a la URL de B no toca a B."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant_a, wa_a = make_wa_tenant(db, label="A")
        tenant_b, wa_b = make_wa_tenant(db, label="B")

    process_evolution_webhook(tenant_b.id, upsert_payload(wa_a.instance_name, msg_id="X-1", text="intruso"))

    with SessionLocal() as db:
        assert _messages(db, tenant_a.id) == []
        assert _messages(db, tenant_b.id) == []
        assert _conversations(db, tenant_b.id) == []


@requires_db
def test_status_update_never_touches_another_tenants_message(wa_offline):
    from app.infrastructure.persistence.database import SessionLocal

    shared_id = f"3EB0{uuid.uuid4().hex[:16].upper()}"
    with SessionLocal() as db:
        tenant_a, wa_a = make_wa_tenant(db, label="A")
        tenant_b, wa_b = make_wa_tenant(db, label="B")

    process_evolution_webhook(tenant_a.id, upsert_payload(wa_a.instance_name, msg_id=shared_id, text="a", from_me=True))
    process_evolution_webhook(tenant_b.id, upsert_payload(wa_b.instance_name, msg_id=shared_id, text="b", from_me=True))

    with SessionLocal() as db:
        update_message_status(db, tenant_a.id, status_payload("", msg_id=shared_id, status="READ")["data"])
        db.commit()

    with SessionLocal() as db:
        assert _messages(db, tenant_a.id)[0].status == "read"
        assert _messages(db, tenant_b.id)[0].status == "sent"


@requires_db
def test_conversation_lookup_never_returns_another_tenants_chat(wa_offline):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant_a, session_a = make_wa_tenant(db, label="A")
        tenant_b, session_b = make_wa_tenant(db, label="B")
        save_inbound_message(
            db,
            tenant=tenant_a,
            evolution_message_id="LOOKUP-1",
            remote_jid="573001112233@s.whatsapp.net",
            body="hola",
            whatsapp_connection_id=session_a.active_connection_id,
            publish=False,
        )
        db.commit()

        # Mismo número, conexión de A, pero preguntando como B: no hay chat.
        for connection_id in (session_a.active_connection_id, session_b.active_connection_id):
            assert (
                find_conversation_for_contact(
                    db,
                    tenant_id=tenant_b.id,
                    whatsapp_connection_id=connection_id,
                    contact_phone="+573001112233",
                    contact_jid="573001112233@s.whatsapp.net",
                )
                is None
            )


@requires_db
def test_webhook_endpoint_requires_the_shared_secret(client):
    response = client.post(
        f"/webhooks/evolution/{uuid.uuid4()}",
        json=upsert_payload("cualquiera", msg_id="S-1", text="hola"),
        headers={"X-Webhook-Secret": "no-es-el-secreto"},
    )
    assert response.status_code == 401


"""Duplicados: reintentos de Evolution, mensaje que llega por webhook y por sync,
ecos del panel y mismo contacto con varias identidades (@s.whatsapp.net / @lid)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.application.messaging.message_service import import_messages_batch
from app.application.messaging.webhook_processor import process_evolution_webhook
from app.domain.entities import Conversation, Message
from tests.conftest import make_wa_tenant, requires_db, upsert_payload


def _msg_id() -> str:
    return f"3EB0{uuid.uuid4().hex[:16].upper()}"


def _messages(db, tenant_id):
    return db.query(Message).filter(Message.tenant_id == tenant_id).order_by(Message.created_at).all()


def _conversations(db, tenant_id):
    return db.query(Conversation).filter(Conversation.tenant_id == tenant_id).all()


@requires_db
def test_evolution_retrying_the_same_webhook_stores_one_message(wa_offline):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
    payload = upsert_payload(wa.instance_name, msg_id=_msg_id(), text="¿Tienen domicilio?")

    for _ in range(3):
        process_evolution_webhook(tenant.id, payload)

    with SessionLocal() as db:
        assert len(_messages(db, tenant.id)) == 1
        [conv] = _conversations(db, tenant.id)
        assert conv.unread_count == 1
    assert len(wa_offline["ai_jobs"]) == 1, "la IA no debe contestar dos veces el mismo mensaje"


@requires_db
def test_message_seen_by_webhook_and_later_by_history_sync_is_not_duplicated(wa_offline):
    from app.infrastructure.persistence.database import SessionLocal

    msg_id = _msg_id()
    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
    payload = upsert_payload(wa.instance_name, msg_id=msg_id, text="Quiero reservar")
    process_evolution_webhook(tenant.id, payload)

    with SessionLocal() as db:
        tenant_row = db.merge(tenant)
        import_messages_batch(
            db,
            tenant=tenant_row,
            data=payload["data"],
            whatsapp_connection_id=wa.active_connection_id,
        )
        db.commit()
        assert [m.evolution_message_id for m in _messages(db, tenant.id)] == [msg_id]


@requires_db
def test_customer_repeating_the_same_text_keeps_both_messages(wa_offline):
    """"hola" + "hola" con ids distintos son dos mensajes reales, no un duplicado."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)

    process_evolution_webhook(tenant.id, upsert_payload(wa.instance_name, msg_id=_msg_id(), text="hola"))
    process_evolution_webhook(tenant.id, upsert_payload(wa.instance_name, msg_id=_msg_id(), text="hola"))

    with SessionLocal() as db:
        assert [m.body for m in _messages(db, tenant.id)] == ["hola", "hola"]
        [conv] = _conversations(db, tenant.id)
        assert conv.unread_count == 2


@requires_db
def test_panel_message_echoed_by_the_phone_is_merged_and_learns_its_whatsapp_id(wa_offline):
    """El panel guarda el mensaje antes de tener id; el eco de WhatsApp no debe duplicarlo
    y el id que trae se guarda para que luego lleguen los checks de entregado/leído."""
    from app.application.messaging.message_service import _save_message, get_or_create_conversation
    from app.domain.entities.enums import MessageDirection, MessageSource, MessageStatus
    from app.infrastructure.persistence.database import SessionLocal

    echo_id = _msg_id()
    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
        conv = get_or_create_conversation(
            db,
            tenant_id=tenant.id,
            contact_phone="+573001112233",
            whatsapp_connection_id=wa.active_connection_id,
        )
        _save_message(
            db,
            tenant=tenant,
            conversation=conv,
            direction=MessageDirection.OUT.value,
            source=MessageSource.AGENT.value,
            body="Tu pedido sale en 20 min",
            status=MessageStatus.SENT.value,
            evolution_message_id="",
            increment_unread=False,
            publish=False,
        )
        db.commit()

    process_evolution_webhook(
        tenant.id,
        upsert_payload(wa.instance_name, msg_id=echo_id, text="Tu pedido sale en 20 min", from_me=True),
    )

    with SessionLocal() as db:
        [message] = _messages(db, tenant.id)
        assert message.evolution_message_id == echo_id


@requires_db
def test_same_contact_arriving_by_phone_and_then_by_lid_stays_in_one_chat(wa_offline):
    """WhatsApp a veces identifica al mismo cliente por @lid; si la key trae la pareja
    verificada (remoteJidAlt) debe caer en el chat que ya existe."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)

    process_evolution_webhook(
        tenant.id, upsert_payload(wa.instance_name, msg_id=_msg_id(), text="hola", phone="573001112233")
    )
    lid_payload = {
        "event": "messages.upsert",
        "instance": wa.instance_name,
        "data": {
            "key": {
                "remoteJid": "190000000000001@lid",
                "remoteJidAlt": "573001112233@s.whatsapp.net",
                "fromMe": False,
                "id": _msg_id(),
            },
            "pushName": "Cliente",
            "message": {"conversation": "¿sigue disponible?"},
        },
    }
    process_evolution_webhook(tenant.id, lid_payload)

    with SessionLocal() as db:
        assert len(_conversations(db, tenant.id)) == 1
        assert len(_messages(db, tenant.id)) == 2


@requires_db
def test_burst_from_many_contacts_creates_exactly_one_chat_per_contact(wa_offline):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)

    phones = [f"5730011100{n:02d}" for n in range(8)]
    for round_ in range(3):
        for phone in phones:
            process_evolution_webhook(
                tenant.id,
                upsert_payload(wa.instance_name, msg_id=_msg_id(), text=f"mensaje {round_}", phone=phone),
            )

    with SessionLocal() as db:
        conversations = _conversations(db, tenant.id)
        assert sorted(c.contact_phone for c in conversations) == sorted(f"+{p}" for p in phones)
        assert len(_messages(db, tenant.id)) == len(phones) * 3


@requires_db
def test_database_rejects_two_chats_for_the_same_number_and_connection():
    """Última línea de defensa si dos procesos crean el chat a la vez."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
        for _ in range(2):
            db.add(
                Conversation(
                    tenant_id=tenant.id,
                    contact_phone="+573001112233",
                    whatsapp_connection_id=wa.active_connection_id,
                )
            )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

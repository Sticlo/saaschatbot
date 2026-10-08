"""Desvincular WhatsApp o fusionar chats borra chats, nunca citas: y cuando el cliente vuelve a
escribir, sus citas regresan a su chat (la IA las ve y el panel puede abrir el chat)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.application.messaging.webhook_processor import process_evolution_webhook
from app.domain.entities import Appointment, Conversation
from tests.conftest import make_wa_tenant, requires_db, upsert_payload


def _msg_id() -> str:
    return f"3EB0{uuid.uuid4().hex[:16].upper()}"


def _conversation(db, tenant_id) -> Conversation:
    return db.query(Conversation).filter(Conversation.tenant_id == tenant_id).one()


def _book_for(db, conversation: Conversation) -> uuid.UUID:
    from app.application.appointments.appointment_service import create_appointment

    starts = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=7)
    row = create_appointment(
        db, tenant_id=conversation.tenant_id, starts_at=starts, ends_at=starts + timedelta(hours=1),
        client_name="Sebastián", client_phone=conversation.contact_phone, conversation_id=conversation.id,
    )
    db.commit()
    return row.id


@requires_db
def test_unlinking_whatsapp_keeps_the_appointments_and_they_return_to_the_new_chat(wa_offline):
    from app.application.conversations.whatsapp_conversation_service import (
        purge_all_tenant_whatsapp_conversations,
    )
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
    process_evolution_webhook(tenant.id, upsert_payload(wa.instance_name, msg_id=_msg_id(), text="hola"))
    with SessionLocal() as db:
        appointment_id = _book_for(db, _conversation(db, tenant.id))
        purge_all_tenant_whatsapp_conversations(db, tenant_id=tenant.id)
        db.commit()

    with SessionLocal() as db:
        row = db.get(Appointment, appointment_id)
        assert row is not None, "desvincular nunca borra citas"
        assert row.conversation_id is None
        assert row.client_name == "Sebastián"

    process_evolution_webhook(
        tenant.id, upsert_payload(wa.instance_name, msg_id=_msg_id(), text="¿a qué hora era mi cita?")
    )

    with SessionLocal() as db:
        conversation = _conversation(db, tenant.id)
        assert db.get(Appointment, appointment_id).conversation_id == conversation.id


@requires_db
def test_merging_duplicate_chats_moves_their_appointments(wa_offline):
    from app.application.conversations.whatsapp_conversation_service import merge_conversations
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
        primary = Conversation(
            tenant_id=tenant.id, contact_phone="+573001112233", contact_name="Sebastián",
            whatsapp_connection_id=wa.active_connection_id,
        )
        secondary = Conversation(
            tenant_id=tenant.id, contact_phone="lid:20637351436488", contact_jid="20637351436488@lid",
            contact_name="", whatsapp_connection_id=wa.active_connection_id,
        )
        db.add_all([primary, secondary])
        db.commit()
        appointment_id = _book_for(db, secondary)

        merge_conversations(db, tenant_id=tenant.id, primary=primary, secondary=secondary)
        db.commit()
        primary_id = primary.id

    with SessionLocal() as db:
        assert db.get(Appointment, appointment_id).conversation_id == primary_id


def test_same_client_matches_phone_variants_and_whatsapp_ids():
    from app.application.appointments.appointment_service import _same_client

    assert _same_client("+573001112233", "+573001112233", "")
    assert _same_client("3001112233", "+573001112233", "")
    assert _same_client("lid:20637351436488", "lid:20637351436488", "")
    assert _same_client("lid:20637351436488", "+573001112233", "20637351436488@lid")
    assert not _same_client("+573001112233", "+573009998877", "")
    assert not _same_client("lid:111", "lid:222", "")
    assert not _same_client("+573001112233", "lid:20637351436488", "")

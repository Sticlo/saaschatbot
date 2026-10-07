"""Pérdida de internet y desincronización: el celular o el servidor se caen y vuelven,
Evolution reenvía eventos tarde, repetidos o desordenados, y el panel debe terminar
mostrando exactamente lo que pasó en WhatsApp."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from app.application.messaging import webhook_processor
from app.application.messaging.webhook_processor import process_evolution_webhook
from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
from tests.conftest import (
    connection_payload,
    make_wa_tenant,
    requires_db,
    status_payload,
    upsert_payload,
)


def _msg_id() -> str:
    return f"3EB0{uuid.uuid4().hex[:16].upper()}"


def _status(db, tenant_id) -> str:
    return db.query(Tenant).filter(Tenant.id == tenant_id).one().whatsapp_status


def _messages(db, tenant_id):
    return db.query(Message).filter(Message.tenant_id == tenant_id).order_by(Message.created_at).all()


# --- Conexión que se cae y vuelve --------------------------------------------------------


@requires_db
def test_connection_dropping_and_returning_several_times_a_day_is_always_tracked(wa_offline):
    """Cada close/open cuenta: el segundo corte del día no puede ignorarse por "duplicado"."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)

    for _ in range(3):
        process_evolution_webhook(tenant.id, connection_payload(wa.instance_name, "close"))
        with SessionLocal() as db:
            assert _status(db, tenant.id) == "disconnected"
        process_evolution_webhook(tenant.id, connection_payload(wa.instance_name, "open"))
        with SessionLocal() as db:
            assert _status(db, tenant.id) == "connected"

    assert wa_offline["sync_after_connect"] == [tenant.id] * 3, "cada reconexión debe resincronizar"


@requires_db
def test_reconnecting_the_same_phone_keeps_the_chats(wa_offline):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
    process_evolution_webhook(tenant.id, upsert_payload(wa.instance_name, msg_id=_msg_id(), text="antes del corte"))

    process_evolution_webhook(tenant.id, connection_payload(wa.instance_name, "close"))
    process_evolution_webhook(tenant.id, connection_payload(wa.instance_name, "open"))

    with SessionLocal() as db:
        session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant.id).one()
        assert session.active_connection_id == wa.active_connection_id
        assert [m.body for m in _messages(db, tenant.id)] == ["antes del corte"]


@requires_db
def test_reconnecting_with_a_different_phone_starts_clean(wa_offline):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db, owner_phone="573150000000")
    process_evolution_webhook(tenant.id, upsert_payload(wa.instance_name, msg_id=_msg_id(), text="chat del celular viejo"))

    process_evolution_webhook(tenant.id, connection_payload(wa.instance_name, "close"))
    process_evolution_webhook(
        tenant.id, connection_payload(wa.instance_name, "open", owner_phone="573159999999")
    )

    with SessionLocal() as db:
        session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant.id).one()
        assert session.active_connection_id != wa.active_connection_id
        assert _messages(db, tenant.id) == []


# --- Mensajes mientras el panel cree que está desconectado -------------------------------


@requires_db
def test_message_arriving_before_the_reconnect_event_is_not_lost(wa_offline):
    """Los mensajes se procesan al instante y `connection.update` va por cola: tras un corte
    el mensaje puede llegar antes que el "open". Si la vinculación sigue intacta, se guarda."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db, connected=False)

    process_evolution_webhook(tenant.id, upsert_payload(wa.instance_name, msg_id=_msg_id(), text="¿siguen abiertos?"))

    with SessionLocal() as db:
        assert [m.body for m in _messages(db, tenant.id)] == ["¿siguen abiertos?"]


@requires_db
def test_messages_after_the_owner_unlinked_whatsapp_are_ignored(wa_offline):
    """Desvincular desde el panel es definitivo: ni mensajes ni IA."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db, connected=False)
        session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant.id).one()
        session.active_connection_id = None
        session.bound_owner_jid = None
        db.commit()

    process_evolution_webhook(tenant.id, upsert_payload(wa.instance_name, msg_id=_msg_id(), text="hola"))

    with SessionLocal() as db:
        assert _messages(db, tenant.id) == []
        assert db.query(Conversation).filter(Conversation.tenant_id == tenant.id).count() == 0
    assert wa_offline["ai_jobs"] == []


# --- Checks de entregado / leído ---------------------------------------------------------


def _send_from_phone(tenant, wa, msg_id: str, text: str = "Listo, ya va") -> None:
    process_evolution_webhook(tenant.id, upsert_payload(wa.instance_name, msg_id=msg_id, text=text, from_me=True))


def _message_status(tenant_id, msg_id: str) -> str:
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        return (
            db.query(Message)
            .filter(Message.tenant_id == tenant_id, Message.evolution_message_id == msg_id)
            .one()
            .status
        )


@requires_db
def test_delivery_and_read_receipts_advance_the_message_status(wa_offline):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
    first, second = _msg_id(), _msg_id()
    _send_from_phone(tenant, wa, first)
    _send_from_phone(tenant, wa, second, text="otro")

    for msg_id in (first, second):
        process_evolution_webhook(tenant.id, status_payload(wa.instance_name, msg_id=msg_id, status="DELIVERY_ACK"))
        assert _message_status(tenant.id, msg_id) == "delivered"
        process_evolution_webhook(tenant.id, status_payload(wa.instance_name, msg_id=msg_id, status="READ"))
        assert _message_status(tenant.id, msg_id) == "read"


@requires_db
def test_late_delivery_receipt_never_downgrades_a_read_message(wa_offline):
    """Tras reconectar, Evolution puede mandar el "entregado" después del "leído"."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
    msg_id = _msg_id()
    _send_from_phone(tenant, wa, msg_id)

    for status in ("READ", "DELIVERY_ACK", "SERVER_ACK"):
        process_evolution_webhook(tenant.id, status_payload(wa.instance_name, msg_id=msg_id, status=status))

    assert _message_status(tenant.id, msg_id) == "read"


@requires_db
def test_voice_note_played_counts_as_read(wa_offline):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
    msg_id = _msg_id()
    _send_from_phone(tenant, wa, msg_id)
    process_evolution_webhook(tenant.id, status_payload(wa.instance_name, msg_id=msg_id, status="PLAYED"))
    assert _message_status(tenant.id, msg_id) == "read"


# --- Mensajes que llegan tarde ------------------------------------------------------------


@requires_db
def test_messages_delivered_late_and_out_of_order_keep_whatsapp_order(wa_offline):
    """Al volver el internet llegan de golpe, en cualquier orden: el chat se ordena por la
    hora real de WhatsApp (messageTimestamp), no por la hora en que llegaron al servidor."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)

    base = int(time.time()) - 3600
    sent = [
        (base + 0, "1. hola", False),
        (base + 60, "2. hola, ¿qué deseas?", True),
        (base + 120, "3. una pizza", False),
        (base + 180, "4. ¿de qué sabor?", True),
    ]
    for ts, text, from_me in reversed(sent):
        process_evolution_webhook(
            tenant.id,
            upsert_payload(wa.instance_name, msg_id=_msg_id(), text=text, from_me=from_me, timestamp=ts),
        )

    with SessionLocal() as db:
        messages = _messages(db, tenant.id)
        assert [m.body for m in messages] == [text for _, text, _ in sent]
        assert [int(m.created_at.timestamp()) for m in messages] == [ts for ts, _, _ in sent]
        conv = db.query(Conversation).filter(Conversation.tenant_id == tenant.id).one()
        assert int(conv.last_message_at.timestamp()) == base + 180


@requires_db
def test_timestamp_from_a_phone_with_a_wrong_clock_is_clamped_to_now(wa_offline):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
    future = int(time.time()) + 7 * 86400

    before = datetime.now(timezone.utc)
    process_evolution_webhook(
        tenant.id, upsert_payload(wa.instance_name, msg_id=_msg_id(), text="hola", timestamp=future)
    )

    with SessionLocal() as db:
        [message] = _messages(db, tenant.id)
        assert before.timestamp() - 1 <= message.created_at.timestamp() <= time.time() + 1


@requires_db
def test_chats_from_before_scanning_the_qr_are_not_imported(wa_offline):
    """Omitel solo atiende lo que llega después de conectar: el historial del celular no
    crea chats ni hace responder a la IA."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
    connected_at = int(wa.connection_started_at.timestamp())

    process_evolution_webhook(
        tenant.id,
        upsert_payload(
            wa.instance_name, msg_id=_msg_id(), text="chat de la semana pasada",
            phone="573009990000", timestamp=connected_at - 7 * 86400,
        ),
    )
    process_evolution_webhook(
        tenant.id,
        upsert_payload(
            wa.instance_name, msg_id=_msg_id(), text="respuesta vieja del dueño",
            phone="573009990000", from_me=True, timestamp=connected_at - 3600,
        ),
    )
    process_evolution_webhook(
        tenant.id,
        upsert_payload(wa.instance_name, msg_id=_msg_id(), text="hola, ¿tienen domicilio?"),
    )

    with SessionLocal() as db:
        assert [m.body for m in _messages(db, tenant.id)] == ["hola, ¿tienen domicilio?"]
        assert db.query(Conversation).filter(Conversation.tenant_id == tenant.id).count() == 1
    assert len(wa_offline["ai_jobs"]) == 1


# --- Fallos a mitad de camino -------------------------------------------------------------


@requires_db
def test_message_that_failed_to_save_is_processed_when_evolution_retries(wa_offline, monkeypatch):
    """Si guardar falla (BD caída un instante), el reintento no puede descartarse como duplicado."""
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
    payload = upsert_payload(wa.instance_name, msg_id=_msg_id(), text="¿me confirman?")

    real_save = webhook_processor.save_inbound_message
    attempts = {"n": 0}

    def flaky_save(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("conexión a la BD interrumpida")
        return real_save(*args, **kwargs)

    monkeypatch.setattr(webhook_processor, "save_inbound_message", flaky_save)

    process_evolution_webhook(tenant.id, payload)
    with SessionLocal() as db:
        assert _messages(db, tenant.id) == []

    process_evolution_webhook(tenant.id, payload)
    with SessionLocal() as db:
        assert [m.body for m in _messages(db, tenant.id)] == ["¿me confirman?"]
    assert len(wa_offline["ai_jobs"]) == 1


def test_extract_whatsapp_id_from_wrapped_send_response():
    from app.application.messaging.message_service import extract_whatsapp_message_id

    assert extract_whatsapp_message_id({"key": {"id": "3EB0ABC"}}) == "3EB0ABC"
    assert extract_whatsapp_message_id({"data": {"key": {"id": "3EB0DEF"}}}) == "3EB0DEF"
    assert extract_whatsapp_message_id({"keyId": "3AB2XYZ", "status": "DELIVERY_ACK"}) == "3AB2XYZ"
    assert extract_whatsapp_message_id({}) is None


@requires_db
def test_messages_update_list_and_device_suffix_still_mark_delivered(wa_offline):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db)
    msg_id = _msg_id()
    _send_from_phone(tenant, wa, msg_id)

    process_evolution_webhook(
        tenant.id,
        {
            "event": "messages.update",
            "instance": wa.instance_name,
            "data": [
                {
                    "keyId": f"{msg_id}:0",
                    "update": {"status": "DELIVERY_ACK"},
                    "remoteJid": "573001112233@s.whatsapp.net",
                    "fromMe": True,
                }
            ],
        },
    )
    assert _message_status(tenant.id, msg_id) == "delivered"

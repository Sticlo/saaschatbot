"""Diagnóstico al vincular: lo que el negocio se pierde en el chat, sacado del historial de WhatsApp."""
from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone

from tests.conftest import make_wa_tenant, requires_db

pytestmark = requires_db

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _at(days_ago: int, hour: int, minute: int = 0) -> datetime:
    from app.application.appointments.appointment_service import BOGOTA

    day = NOW.astimezone(BOGOTA).date() - timedelta(days=days_ago)
    return datetime.combine(day, time(hour, minute), tzinfo=BOGOTA).astimezone(timezone.utc)


def _msg(jid: str, text: str | None, when: datetime, *, from_me: bool = False, sticker: bool = False) -> dict:
    message = {"stickerMessage": {"mimetype": "image/webp"}} if sticker else {"conversation": text}
    return {
        "key": {"remoteJid": jid, "fromMe": from_me, "id": uuid.uuid4().hex[:16].upper()},
        "message": message,
        "messageTimestamp": int(when.timestamp()),
    }


def _personal_chats() -> list[dict]:
    """Un número mixto: la familia y el banco también escriben, y no son clientes."""
    family, friend, bank = (f"57311{n}000000@s.whatsapp.net" for n in range(1, 4))
    chatty = [
        _msg(family, "jaja sí" if i % 2 else "¿cuánto vale el arriendo este mes?", _at(5, 8) + timedelta(minutes=i), from_me=bool(i % 3 == 0))
        for i in range(300)
    ]
    return [
        *chatty,
        _msg(friend, "Hola mami, cómo estás? ya llegaste?", _at(2, 21)),
        _msg(bank, "Bancolombia: recibiste una transferencia por $50.000. Detalle en https://banco.co/x", _at(2, 23)),
    ]


def _history() -> dict:
    a, b, c, d = (f"57300{n}000000@s.whatsapp.net" for n in range(1, 5))
    return {
        "messages": [
            *_personal_chats(),
            # Contestado a las 2 horas; el «gracias» final no deja a nadie esperando.
            _msg(a, "Hola, cuánto vale?", _at(3, 10)),
            _msg(a, "Vale $90.000", _at(3, 12), from_me=True),
            _msg(a, "gracias", _at(3, 12, 5)),
            # Escribió de noche y nadie le contestó.
            _msg(b, "tienen talla 38?", _at(2, 22)),
            # Contestado en 3 minutos.
            _msg(c, "Dónde quedan y cuánto cuesta el envío?", _at(1, 15)),
            _msg(c, "En el centro", _at(1, 15, 3), from_me=True),
            # Solo un sticker: no pide respuesta.
            _msg(d, None, _at(1, 16), sticker=True),
            # No cuentan: grupos, el chat del dueño consigo mismo, ni lo de hace más de 30 días.
            _msg("120363000000000000@g.us", "precio?", _at(1, 9)),
            _msg("573150000000@s.whatsapp.net", "nota para mí", _at(1, 9)),
            _msg(a, "precio?", _at(40, 10)),
        ]
    }


def test_history_becomes_a_diagnostic_without_storing_texts():
    from app.application.whatsapp.whatsapp_diagnostic_service import compute_diagnostic, record_history_batch
    from app.domain.entities import Tenant
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, session = make_wa_tenant(db, label="Diag")
        assert record_history_batch(session, _history(), now=NOW) == 7 + 302
        result = compute_diagnostic(db, db.get(Tenant, tenant.id), session.active_connection_id, now=NOW)

    # Solo quienes preguntaron por lo que vende: ni la familia, ni el banco, ni el sticker suelto.
    assert result["clients"] == 3
    assert result["requests"] == 3
    assert result["unanswered"] == 1
    assert result["after_hours"] == 1
    assert result["median_reply_seconds"] == (2 * 3600 + 3 * 60) // 2
    assert result["slow_replies"] == 1
    assert result["fast_reply_share"] == 0.5
    assert result["topics"] == [{"key": "precio", "label": "precios", "chats": 2}]
    assert "talla" not in str(result) and "57300" not in str(result)


def test_status_waits_for_history_then_keeps_the_result():
    from app.application.whatsapp.whatsapp_diagnostic_service import diagnostic_status, record_history_batch
    from app.domain.entities import Tenant, TenantProfile
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, session = make_wa_tenant(db, label="Diag")
        db.add(TenantProfile(tenant_id=tenant.id, avg_ticket_cop=90000))
        db.commit()
        record_history_batch(session, _history(), now=NOW)
        tenant_id = tenant.id
        tenant = db.get(Tenant, tenant_id)

        assert diagnostic_status(db, tenant, now=NOW + timedelta(seconds=5)) == {"status": "analyzing"}
        ready = diagnostic_status(db, tenant, now=NOW + timedelta(seconds=60))

    assert ready["status"] == "ready"
    assert ready["avg_ticket_cop"] == 90000
    assert ready["diagnostic"]["unanswered"] == 1
    with SessionLocal() as db:
        stored = db.query(TenantProfile).filter(TenantProfile.tenant_id == tenant_id).one().whatsapp_diagnostic
    assert stored["connection_id"] == str(session.active_connection_id)


def test_no_history_means_nothing_to_show():
    from app.application.whatsapp.whatsapp_diagnostic_service import diagnostic_status
    from app.domain.entities import Tenant, WhatsAppSession
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, _ = make_wa_tenant(db, label="Diag")
        tenant = db.get(Tenant, tenant.id)
        # Recién vinculado: todavía puede llegar el historial.
        row = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant.id).one()
        row.connection_started_at = NOW - timedelta(seconds=30)
        db.commit()
        assert diagnostic_status(db, tenant, now=NOW)["status"] == "analyzing"
        assert diagnostic_status(db, tenant, now=NOW + timedelta(minutes=10))["status"] == "none"


def test_messages_set_webhook_feeds_the_diagnostic(wa_offline):
    from app.application.messaging.webhook_processor import process_evolution_webhook
    from app.application.whatsapp.whatsapp_diagnostic_service import compute_diagnostic
    from app.domain.entities import Tenant
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, session = make_wa_tenant(db, label="Diag")
    process_evolution_webhook(
        tenant.id, {"event": "messages.set", "instance": session.instance_name, "data": _history()}
    )
    with SessionLocal() as db:
        result = compute_diagnostic(db, db.get(Tenant, tenant.id), session.active_connection_id)
    assert result is not None and result["clients"] == 3


def test_diagnostic_counted_with_old_rules_is_not_shown():
    from app.application.whatsapp.whatsapp_diagnostic_service import diagnostic_status
    from app.domain.entities import Tenant, TenantProfile
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, session = make_wa_tenant(db, label="Diag")
        db.add(
            TenantProfile(
                tenant_id=tenant.id,
                whatsapp_diagnostic={
                    "connection_id": str(session.active_connection_id),
                    "computed_at": NOW.isoformat(),
                    "clients": 86,
                    "unanswered": 54,
                },
            )
        )
        db.commit()
        assert diagnostic_status(db, db.get(Tenant, tenant.id), now=NOW)["status"] == "none"

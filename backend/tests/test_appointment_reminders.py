"""Recordatorio de la cita 2 horas antes, una sola vez, y solo a quien ya le escribió al negocio."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from tests.conftest import requires_db
from tests.test_ai_followup import _setup

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # 10:00 a. m. en Colombia
PHONE = "+573001112233"


def _appointment(db, tenant, client, *, in_hours: float, booked_hours_ago: float = 24, conversation=None, phone=None):
    from app.domain.entities import Appointment

    starts = NOW + timedelta(hours=in_hours)
    row = Appointment(
        tenant_id=tenant.id,
        conversation_id=conversation.id if conversation else None,
        starts_at=starts,
        ends_at=starts + timedelta(hours=1),
        client_name=client,
        client_phone=phone,
        created_at=NOW - timedelta(hours=booked_hours_ago),
    )
    db.add(row)
    db.flush()
    return row


def _conversation(db, tenant, phone=None):
    from app.domain.entities import Conversation

    row = Conversation(
        tenant_id=tenant.id,
        contact_phone=phone or f"+57300{uuid.uuid4().int % 10_000_000:07d}",
        contact_name="Laura",
        last_message_at=NOW - timedelta(days=1),
    )
    db.add(row)
    db.flush()
    return row


@requires_db
def test_only_appointments_in_the_next_two_hours_booked_earlier_are_due():
    from app.application.appointments.appointment_reminder_service import due_reminders
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _setup(db)
        _appointment(db, tenant, "En 1 h 30", in_hours=1.5)
        _appointment(db, tenant, "En 2 h justas", in_hours=2)
        _appointment(db, tenant, "En 3 h", in_hours=3)
        _appointment(db, tenant, "Ya casi empieza", in_hours=0.25)
        _appointment(db, tenant, "Agendó hace un rato", in_hours=1.5, booked_hours_ago=0.25)
        reminded = _appointment(db, tenant, "Ya se le recordó", in_hours=1)
        reminded.reminder_sent_at = NOW - timedelta(minutes=30)
        db.flush()

        names = {a.client_name for a in due_reminders(db, tenant, NOW)}
        assert names == {"En 1 h 30", "En 2 h justas"}
        db.rollback()


@requires_db
def test_reminder_goes_once_to_the_client_chat_and_never_cold(monkeypatch):
    from app.application.appointments import appointment_reminder_service as svc
    from app.application.whatsapp import whatsapp_service
    from app.infrastructure.cache.redis_client import get_redis
    from app.infrastructure.persistence.database import SessionLocal

    sent: list[tuple] = []
    monkeypatch.setattr(
        whatsapp_service,
        "send_text_message",
        lambda db, **kw: sent.append((kw["conversation"].id, kw["text"], kw["source"])),
    )

    with SessionLocal() as db:
        tenant = _setup(db)
        chat = _conversation(db, tenant)
        by_ai = _appointment(db, tenant, "Laura Gómez", in_hours=1.5, conversation=chat)
        by_phone_chat = _conversation(db, tenant, phone=PHONE)
        from_panel = _appointment(db, tenant, "Pedro", in_hours=1.5, phone="300 111 2233")
        stranger = _appointment(db, tenant, "Desconocido", in_hours=1.5, phone="+573009998877")
        try:
            assert svc.send_reminder(db, tenant, by_ai, NOW) is True
            assert svc.send_reminder(db, tenant, from_panel, NOW) is True
            assert svc.send_reminder(db, tenant, stranger, NOW) is False

            assert sent == [
                (chat.id, "Hola Laura, te recordamos tu cita hoy a las 11:30 a. m. en Barbería Norte. "
                 "Si necesitas cambiarla, respóndenos por aquí.", "bot"),
                (by_phone_chat.id, "Hola Pedro, te recordamos tu cita hoy a las 11:30 a. m. en Barbería Norte. "
                 "Si necesitas cambiarla, respóndenos por aquí.", "bot"),
            ]
            assert by_ai.reminder_sent_at == NOW and stranger.reminder_sent_at is None
            assert chat.followup_sent_at == NOW
            assert {a.client_name for a in svc.due_reminders(db, tenant, NOW)} == {"Desconocido"}
        finally:
            get_redis().delete(*(f"appt:reminder:{a.id}" for a in (by_ai, from_panel, stranger)))


@requires_db
def test_business_can_turn_reminders_off_and_nothing_goes_out_at_night(monkeypatch):
    from app.application.appointments import appointment_reminder_service as svc
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _setup(db)
        tenant.profile.appointment_reminders = False
        db.commit()
        assert tenant.id not in {t.id for t in svc._tenants_with_reminders(db)}

    night = datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc)  # 10:00 p. m. en Colombia
    assert svc.within_reminder_hours(night) is False
    assert svc.sweep_once(night) == 0


@requires_db
def test_rescheduling_the_appointment_sends_a_new_reminder():
    from app.application.appointments.appointment_service import update_appointment
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _setup(db)
        row = _appointment(db, tenant, "Laura", in_hours=1.5)
        row.reminder_sent_at = NOW
        db.flush()
        update_appointment(db, tenant_id=tenant.id, appointment_id=row.id, notes="Trae la foto del corte")
        assert row.reminder_sent_at == NOW

        later = row.starts_at + timedelta(days=1)
        update_appointment(
            db, tenant_id=tenant.id, appointment_id=row.id, starts_at=later, ends_at=later + timedelta(hours=1)
        )
        assert row.reminder_sent_at is None
        db.rollback()

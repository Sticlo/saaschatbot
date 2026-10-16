"""Resumen de la mañana: «Anoche tu IA atendió a N clientes y hoy tienes M citas»."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from tests.conftest import requires_db
from tests.test_ai_followup import _setup

NOW = datetime(2026, 10, 14, 12, 30, tzinfo=timezone.utc)  # 7:30 a. m. en Colombia


def _chat_with_bot_reply(db, tenant, *, replied_at, interest=None, client_wrote=True):
    from app.domain.entities import Conversation, Message

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
        contact_name="Cliente",
        interest_status=interest,
    )
    db.add(conversation)
    db.flush()
    turns = [("in", "contact", -1), ("out", "bot", 0), ("out", "bot", 3)] if client_wrote else [("out", "bot", 0)]
    for direction, source, minutes in turns:
        db.add(
            Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                direction=direction,
                source=source,
                body="Hola, ¿en qué te ayudo?",
                status="sent",
                created_at=replied_at + timedelta(minutes=minutes),
            )
        )
    db.flush()


def _appointment(db, tenant, starts_at):
    from app.domain.entities import Appointment

    db.add(Appointment(tenant_id=tenant.id, starts_at=starts_at, ends_at=starts_at + timedelta(hours=1), client_name="C"))
    db.flush()


@requires_db
def test_morning_summary_counts_last_night_and_today():
    from app.application.results.morning_summary import morning_summary
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _setup(db)
        _chat_with_bot_reply(db, tenant, replied_at=NOW - timedelta(hours=10), interest="interested")  # 9:30 p. m.
        _chat_with_bot_reply(db, tenant, replied_at=NOW - timedelta(hours=7), interest="interested")  # 0:30 a. m.
        _chat_with_bot_reply(db, tenant, replied_at=NOW - timedelta(hours=2))  # 5:30 a. m.
        _chat_with_bot_reply(db, tenant, replied_at=NOW - timedelta(hours=14))  # 5:30 p. m. de ayer: no cuenta
        _chat_with_bot_reply(db, tenant, replied_at=NOW - timedelta(hours=11), client_wrote=False)  # recordatorio
        _appointment(db, tenant, NOW - timedelta(hours=1))  # 6:30 a. m. de hoy, ya pasó
        _appointment(db, tenant, NOW + timedelta(hours=2))  # 9:30 a. m.
        _appointment(db, tenant, NOW + timedelta(hours=6))  # 1:30 p. m.
        _appointment(db, tenant, NOW + timedelta(days=1))  # mañana: no cuenta

        assert morning_summary(db, tenant.id, NOW) == {
            "date": "2026-10-14",
            "attended": 3,
            "interested": 2,
            "appointments_today": 3,
            "next_appointment_at": "9:30 a. m.",
        }
        db.rollback()

from __future__ import annotations

import uuid

import pytest

from tests.conftest import requires_db
from tests.test_ai_followup import NOW, _chat, _setup


def _bare_chat(db, tenant):
    from app.domain.entities import Conversation

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
        contact_name="Sin mensajes",
        last_message_at=NOW,
    )
    db.add(conversation)
    db.flush()
    return conversation


@requires_db
def test_free_messages_only_inside_the_24_hour_window():
    from app.application.whatsapp.messaging_policy import KIND_UTILITY, MessagingPolicyError, ensure_can_message
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _setup(db)
        fresh = _chat(db, tenant, "Escribió hace 23 h", history=("contact",), silent_hours=23)
        stale = _chat(db, tenant, "Escribió hace 25 h", history=("contact",), silent_hours=25)

        ensure_can_message(db, fresh, now=NOW)
        with pytest.raises(MessagingPolicyError) as err:
            ensure_can_message(db, stale, now=NOW)
        assert err.value.status_code == 403
        # Su cita o su pedido sí se le avisan, como una plantilla de utilidad de la API oficial.
        ensure_can_message(db, stale, kind=KIND_UTILITY, now=NOW)
        db.rollback()


@requires_db
def test_never_writes_first_to_someone_who_never_wrote():
    from app.application.whatsapp.messaging_policy import KIND_UTILITY, MessagingPolicyError, ensure_can_message
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _setup(db)
        bait_only = _chat(db, tenant, "Solo recibió", history=("bait",), silent_hours=1)
        empty = _bare_chat(db, tenant)
        for conversation in (bait_only, empty):
            for kind in ("reply", KIND_UTILITY):
                with pytest.raises(MessagingPolicyError):
                    ensure_can_message(db, conversation, kind=kind, now=NOW)
        db.rollback()


@requires_db
def test_rules_can_be_switched_off(monkeypatch):
    from app.application.whatsapp.messaging_policy import ensure_can_message
    from app.config import settings
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr(settings, "whatsapp_official_rules", False)
    with SessionLocal() as db:
        tenant = _setup(db)
        ensure_can_message(db, _bare_chat(db, tenant), now=NOW)
        db.rollback()


@requires_db
def test_blocked_message_never_reaches_whatsapp(monkeypatch):
    from app.application.whatsapp import whatsapp_service
    from app.application.whatsapp.messaging_policy import MessagingPolicyError
    from app.infrastructure.persistence.database import SessionLocal

    def fail(*_a, **_k):
        raise AssertionError("no debía salir")

    monkeypatch.setattr(whatsapp_service, "gateway_send_text", fail)
    with SessionLocal() as db:
        tenant = _setup(db)
        stale = _chat(db, tenant, "Hace dos días", history=("contact", "bot"), silent_hours=48)
        with pytest.raises(MessagingPolicyError):
            whatsapp_service.send_text_message(
                db,
                tenant=tenant,
                session=tenant.whatsapp_session,
                conversation=stale,
                text="Hola, ¿sigues interesado?",
                source="agent",
            )
        db.rollback()


@requires_db
def test_team_phone_counts_as_written_only_after_it_writes():
    from app.application.whatsapp.messaging_policy import phone_has_written
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _setup(db)
        wrote = _chat(db, tenant, "Dueño que saludó", history=("contact",), silent_hours=500)
        silent = _chat(db, tenant, "Solo recibió", history=("bait",), silent_hours=1)
        local = wrote.contact_phone[-10:]
        assert phone_has_written(db, tenant.id, f"+57 {local[:3]} {local[3:]}")
        assert not phone_has_written(db, tenant.id, silent.contact_phone)
        assert not phone_has_written(db, tenant.id, "+573001112233")
        db.rollback()


@requires_db
def test_alert_to_a_phone_that_never_wrote_never_reaches_whatsapp(monkeypatch):
    from app.application.conversations import interest_alert_service
    from app.application.whatsapp import whatsapp_gateway
    from app.infrastructure.persistence.database import SessionLocal

    def fail(*_a, **_k):
        raise AssertionError("no debía salir")

    monkeypatch.setattr(whatsapp_gateway, "send_text", fail)
    with SessionLocal() as db:
        tenant = _setup(db)
        assert interest_alert_service.send_alerts(tenant.whatsapp_session, ["+573001112233"], "🔔 aviso") == 0
        db.rollback()


@requires_db
def test_reply_only_mode_sends_nothing_on_its_own(monkeypatch):
    from app.application.ai import ai_followup_service
    from app.application.appointments import appointment_reminder_service
    from app.application.whatsapp.messaging_policy import (
        KIND_UTILITY,
        MessagingPolicyError,
        ensure_can_alert,
        ensure_can_message,
    )
    from app.config import settings
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr(settings, "whatsapp_proactive_messages", False)
    assert ai_followup_service.sweep_once(NOW, gap=None) == 0
    assert appointment_reminder_service.sweep_once(NOW, gap=None) == 0
    with pytest.raises(MessagingPolicyError):
        ensure_can_alert(uuid.uuid4(), "+573001112233")
    with SessionLocal() as db:
        tenant = _setup(db)
        fresh = _chat(db, tenant, "Escribió hace 2 h", history=("contact",), silent_hours=2)
        stale = _chat(db, tenant, "Escribió hace 2 días", history=("contact",), silent_hours=48)
        ensure_can_message(db, fresh, kind=KIND_UTILITY, now=NOW)
        with pytest.raises(MessagingPolicyError):
            ensure_can_message(db, stale, kind=KIND_UTILITY, now=NOW)
        db.rollback()


@requires_db
def test_followup_is_not_due_when_the_window_is_about_to_close():
    from app.application.ai.ai_followup_service import due_followups
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _setup(db, followup_hours=12)
        _chat(db, tenant, "Calló hace 13 h", history=("contact", "bot"), silent_hours=13)
        _chat(db, tenant, "Le quedan minutos", history=("contact", "bot"), silent_hours=23.8)
        names = {c.contact_name for c in due_followups(db, tenant, NOW)}
        assert names == {"Calló hace 13 h"}
        db.rollback()

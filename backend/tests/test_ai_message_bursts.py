"""El cliente escribe en varios mensajes seguidos: la IA espera a que termine y contesta todo junto."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.conftest import requires_db

pytestmark = requires_db


def _seed(db, rows: list[tuple[str, str, float]]):
    """rows: [(direction, body, segundos_atrás)] en orden."""
    from app.domain.entities import Conversation, Message, Tenant, TenantProfile, WhatsAppSession
    from app.domain.entities.enums import AiMode, ConversationMode, MessageSource, WhatsAppStatus

    tenant = Tenant(business_name="KatShoes", slug=f"k-{uuid.uuid4().hex[:8]}", ai_global_enabled=True)
    db.add(tenant)
    db.flush()
    db.add(TenantProfile(tenant_id=tenant.id, ai_mode=AiMode.QUALIFY.value))
    db.add(
        WhatsAppSession(
            tenant_id=tenant.id,
            instance_name=f"inst_{uuid.uuid4().hex[:8]}",
            status=WhatsAppStatus.CONNECTED.value,
        )
    )
    conv = Conversation(
        tenant_id=tenant.id,
        contact_phone=f"57300{uuid.uuid4().int % 10**7:07d}",
        contact_name="Laura",
        ai_active=True,
        mode=ConversationMode.AUTO.value,
        whatsapp_connection_id=uuid.uuid4(),
    )
    db.add(conv)
    db.flush()
    now = datetime.now(timezone.utc)
    messages = []
    for direction, body, ago in rows:
        row = Message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            direction=direction,
            source=MessageSource.CONTACT.value if direction == "in" else MessageSource.BOT.value,
            body=body,
            status="received",
            created_at=now - timedelta(seconds=ago),
        )
        db.add(row)
        messages.append(row)
    db.commit()
    return tenant, conv, messages


def _run_all(db, tenant, conv, messages):
    from app.application.ai.ai_service import process_ai_reply

    for msg in messages:
        if msg.direction == "in":
            process_ai_reply(db, tenant_id=tenant.id, conversation_id=conv.id, message_id=msg.id)
            db.commit()


@pytest.fixture()
def ai(monkeypatch):
    from app.application.ai import ai_queue_service, ai_service
    from app.application.ai.ai_shortcut_service import AiGeneratedReply
    from app.config import settings

    monkeypatch.setattr(settings, "ai_burst_quiet_seconds", 8.0)
    monkeypatch.setattr(settings, "ai_burst_max_wait_seconds", 30.0)
    calls = {"generated": [], "sent": [], "classified": [], "retries": [], "on_generate": None}

    def fake_generate(**kw):
        calls["generated"].append(kw)
        if calls["on_generate"]:
            calls["on_generate"]()
        return AiGeneratedReply(message="¡Hola Laura! El envío a Medellín vale $12.000.")

    def fake_classify(text, **_kw):
        calls["classified"].append(text)
        return {"category": "duda"}

    monkeypatch.setattr(ai_service, "generate_qualify_reply", fake_generate)
    monkeypatch.setattr(ai_service, "send_reply_with_shortcut", lambda *_a, **kw: calls["sent"].append(kw["reply"]))
    monkeypatch.setattr(ai_service, "send_text_message", lambda *_a, **_kw: None)
    monkeypatch.setattr(ai_service, "classify_inbound_message", fake_classify)
    monkeypatch.setattr(ai_service.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        ai_queue_service,
        "schedule_ai_retry",
        lambda *_a, delay_seconds, **_kw: calls["retries"].append(delay_seconds),
    )
    return calls


def test_waits_while_the_client_is_still_writing(ai):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, rows = _seed(db, [("in", "hola", 2)])
        _run_all(db, tenant, conv, rows)

    assert ai["generated"] == []
    # «hola» casi nunca es todo: se espera más que con una pregunta completa.
    assert len(ai["retries"]) == 1 and 8 < ai["retries"][0] <= 12


def test_a_full_question_waits_less(ai):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, rows = _seed(db, [("in", "¿Cuánto vale el envío a Medellín?", 1)])
        _run_all(db, tenant, conv, rows)

    assert len(ai["retries"]) == 1 and ai["retries"][0] <= 5


def test_burst_is_answered_once_with_everything_the_client_said(ai):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, rows = _seed(
            db,
            [
                ("out", "¡Hola! ¿En qué te ayudo?", 120),
                ("in", "hola", 40),
                ("in", "una pregunta", 36),
                ("in", "cuánto vale el envío a Medellín?", 30),
            ],
        )
        _run_all(db, tenant, conv, rows)

    assert ai["retries"] == []
    assert len(ai["generated"]) == 1
    assert len(ai["sent"]) == 1
    assert ai["classified"] == ["hola\nuna pregunta\ncuánto vale el envío a Medellín?"]


def test_client_who_never_stops_still_gets_an_answer(ai):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, rows = _seed(db, [("in", f"parte {i}", 31 - i * 5) for i in range(7)])
        _run_all(db, tenant, conv, rows)

    # El último llegó hace 1 s, pero la ráfaga empezó hace 31 s: se pasa del tope y se responde.
    assert len(ai["sent"]) == 1


def test_old_unanswered_message_does_not_cut_a_new_burst_short(ai):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, rows = _seed(db, [("in", "hola?", 3600), ("in", "buenas", 1)])
        _run_all(db, tenant, conv, rows)

    assert ai["sent"] == []
    assert len(ai["retries"]) == 1 and ai["retries"][0] > 5


def test_reply_is_dropped_if_the_client_writes_while_it_is_generated(ai):
    from app.domain.entities import Message
    from app.domain.entities.enums import MessageSource
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, rows = _seed(db, [("in", "hola, cuánto vale el envío a Medellín?", 30)])

        def client_writes_again():
            with SessionLocal() as other:
                other.add(
                    Message(
                        tenant_id=tenant.id,
                        conversation_id=conv.id,
                        direction="in",
                        source=MessageSource.CONTACT.value,
                        body="y a Bogotá?",
                        status="received",
                        created_at=datetime.now(timezone.utc),
                    )
                )
                other.commit()

        ai["on_generate"] = client_writes_again
        _run_all(db, tenant, conv, rows)

    assert len(ai["generated"]) == 1
    assert ai["sent"] == []


def test_several_opening_messages_still_count_as_first_contact(ai):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, rows = _seed(db, [("in", "hola", 40), ("in", "buenas tardes", 35), ("in", "tienen tenis?", 30)])
        _run_all(db, tenant, conv, rows)

    assert len(ai["generated"]) == 1
    assert ai["generated"][0]["is_first_contact"] is True


def test_history_limit_counts_turns_not_messages():
    from app.application.ai.ai_conversation_service import format_history

    rows = [
        SimpleNamespace(direction="in", body="hola", transcript=None),
        SimpleNamespace(direction="out", body="¡Hola! Tenemos tenis desde $90.000", transcript=None),
        *[SimpleNamespace(direction="in", body=f"parte {i}", transcript=None) for i in range(10)],
    ]
    history = format_history(rows, limit=10)
    assert {"role": "assistant", "content": "¡Hola! Tenemos tenis desde $90.000"} in history
    assert len(history) == 12


def test_busy_chat_reschedules_the_new_message_instead_of_dropping_it(monkeypatch):
    from app.application.ai import ai_queue_service

    retries = []
    monkeypatch.setattr(ai_queue_service, "_try_acquire_ai_lock", lambda _c: False)
    monkeypatch.setattr(
        ai_queue_service,
        "schedule_ai_retry",
        lambda *_a, delay_seconds, **_kw: retries.append(delay_seconds),
    )
    ai_queue_service._process_ai_job(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    assert retries == [ai_queue_service.BUSY_CHAT_RETRY_SECONDS]

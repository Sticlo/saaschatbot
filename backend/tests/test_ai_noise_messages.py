"""Stickers, reacciones y adjuntos sin texto nunca deben dejar al cliente sin respuesta."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tests.conftest import requires_db

pytestmark = requires_db


def _seed(db, inbound: list[tuple[str, str]], *, transcripts: dict[int, str] | None = None):
    """inbound: [(direction, body)] en orden; devuelve tenant, conv y los mensajes."""
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
        contact_phone="lid:94038325780658",
        contact_name="Laura",
        ai_active=True,
        mode=ConversationMode.AUTO.value,
        whatsapp_connection_id=uuid.uuid4(),
    )
    db.add(conv)
    db.flush()
    start = datetime.now(timezone.utc) - timedelta(seconds=30)
    rows = []
    for i, (direction, body) in enumerate(inbound):
        row = Message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            direction=direction,
            source=MessageSource.CONTACT.value if direction == "in" else MessageSource.BOT.value,
            body=body,
            status="received",
            transcript=(transcripts or {}).get(i),
            created_at=start + timedelta(seconds=i * 5),
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return tenant, conv, rows


def _run(db, tenant, conv, msg):
    from app.application.ai.ai_service import process_ai_reply

    process_ai_reply(db, tenant_id=tenant.id, conversation_id=conv.id, message_id=msg.id)
    db.commit()


@pytest.fixture()
def ai(monkeypatch):
    from app.application.ai import ai_queue_service, ai_service
    from app.application.ai.ai_shortcut_service import AiGeneratedReply

    calls = {"generated": 0, "sent": [], "texts": [], "retries": []}

    def fake_generate(**_kw):
        calls["generated"] += 1
        return AiGeneratedReply(message="¡Hola Laura! ¿En qué te ayudo?")

    monkeypatch.setattr(ai_service, "generate_qualify_reply", fake_generate)
    monkeypatch.setattr(ai_service, "send_reply_with_shortcut", lambda *_a, **kw: calls["sent"].append(kw["reply"]))
    monkeypatch.setattr(ai_service, "send_text_message", lambda *_a, **kw: calls["texts"].append(kw["text"]))
    monkeypatch.setattr(ai_service, "classify_inbound_message", lambda *_a, **_kw: {"category": "duda"})
    monkeypatch.setattr(ai_service.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ai_queue_service, "schedule_ai_retry", lambda *_a, delay_seconds: calls["retries"].append(delay_seconds))
    return calls


def test_hola_then_sticker_still_gets_answered(ai):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, (hola, sticker) = _seed(db, [("in", "hola"), ("in", "[sticker]")])
        _run(db, tenant, conv, hola)
        _run(db, tenant, conv, sticker)

    assert ai["generated"] == 1
    assert len(ai["sent"]) == 1


def test_chat_opened_with_only_a_sticker_gets_a_greeting(ai, monkeypatch):
    from app.application.ai import ai_service
    from app.infrastructure.persistence.database import SessionLocal

    # El clasificador diría «ruido»: el saludo no debe pasar por él.
    monkeypatch.setattr(ai_service, "classify_inbound_message", lambda *_a, **_kw: {"category": "ruido"})
    with SessionLocal() as db:
        tenant, conv, (sticker,) = _seed(db, [("in", "[sticker]")])
        _run(db, tenant, conv, sticker)

    assert ai["generated"] == 1
    assert len(ai["sent"]) == 1


def test_sticker_after_bot_answer_is_not_answered(ai):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, rows = _seed(
            db, [("in", "tienes tacones?"), ("out", "Sí, tenemos tacones."), ("in", "[sticker]")]
        )
        _run(db, tenant, conv, rows[-1])

    assert ai["generated"] == 0
    assert ai["sent"] == [] and ai["texts"] == []


def test_inaudible_audio_asks_to_write_it(ai, monkeypatch):
    from app.application.ai import ai_service
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr(ai_service.gemini_client, "is_configured", lambda: True)
    with SessionLocal() as db:
        tenant, conv, (audio,) = _seed(db, [("in", "[audio]")], transcripts={0: ""})
        _run(db, tenant, conv, audio)

    assert ai["texts"] == [ai_service.UNREADABLE_AUDIO_REPLY]
    assert ai["generated"] == 0


def test_audio_that_could_not_be_processed_is_retried(ai, monkeypatch):
    from app.application.ai import ai_service
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr(ai_service.gemini_client, "is_configured", lambda: True)
    monkeypatch.setattr(ai_service, "ensure_transcript", lambda *_a, **_kw: None)
    with SessionLocal() as db:
        tenant, conv, (audio,) = _seed(db, [("in", "[audio]")])
        _run(db, tenant, conv, audio)

    assert ai["retries"] == [ai_service.OUTAGE_RETRY_DELAYS_SECONDS[0]]
    assert ai["texts"] == []


def test_history_shows_stickers_in_plain_words():
    from types import SimpleNamespace

    from app.application.ai.ai_conversation_service import format_history

    rows = [
        SimpleNamespace(direction="in", body="hola", transcript=None),
        SimpleNamespace(direction="in", body="[sticker]", transcript=None),
    ]
    assert format_history(rows)[-1] == {"role": "user", "content": "(envió un sticker sin texto)"}

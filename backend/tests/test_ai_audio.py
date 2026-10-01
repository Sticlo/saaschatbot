from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest

from tests.conftest import requires_db


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _gemini_reply(*parts: dict) -> dict:
    return {"candidates": [{"content": {"parts": list(parts)}}]}


@pytest.fixture
def gemini_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    return settings


def test_transcribe_sends_base_mime_and_skips_thoughts(monkeypatch, gemini_key):
    from app.infrastructure.ai import gemini_client

    seen: dict = {}

    def fake_post(self, url, json=None, headers=None):
        seen.update(url=url, json=json, headers=headers)
        return _FakeResponse(
            200,
            _gemini_reply({"text": "pensando…", "thought": True}, {"text": " ¿Tienen habitación para hoy? "}),
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    text = gemini_client.transcribe_audio("QUJD", "audio/ogg; codecs=opus")

    assert text == "¿Tienen habitación para hoy?"
    assert seen["url"].endswith(f"/models/{gemini_key.gemini_audio_model}:generateContent")
    assert seen["headers"]["x-goog-api-key"] == "test-key"
    inline = seen["json"]["contents"][0]["parts"][1]["inline_data"]
    assert inline == {"mime_type": "audio/ogg", "data": "QUJD"}


def test_transcribe_inaudible_and_errors(monkeypatch, gemini_key):
    from app.infrastructure.ai import gemini_client

    monkeypatch.setattr(httpx.Client, "post", lambda *_a, **_kw: _FakeResponse(200, _gemini_reply({"text": "[inaudible]"})))
    assert gemini_client.transcribe_audio("QUJD", "audio/ogg") == ""

    monkeypatch.setattr(httpx.Client, "post", lambda *_a, **_kw: _FakeResponse(403, text="denied"))
    with pytest.raises(gemini_client.GeminiError, match="GEMINI_API_KEY"):
        gemini_client.transcribe_audio("QUJD", "audio/ogg")


def test_transcribe_requires_key(monkeypatch):
    from app.config import settings
    from app.infrastructure.ai import gemini_client

    monkeypatch.setattr(settings, "gemini_api_key", "<tu-api-key-gemini>")
    assert gemini_client.is_configured() is False
    with pytest.raises(gemini_client.GeminiError):
        gemini_client.transcribe_audio("QUJD", "audio/ogg")


def test_history_and_ai_text_use_transcript():
    from app.application.ai.ai_conversation_service import format_history
    from app.application.messaging.message_service import text_for_ai

    voice = SimpleNamespace(direction="in", body="[audio]", transcript="Quiero reservar para el sábado")
    plain = SimpleNamespace(direction="out", body="¡Hola!", transcript=None)
    assert text_for_ai(voice) == "Quiero reservar para el sábado"
    assert text_for_ai(plain) == "¡Hola!"
    assert format_history([plain, voice]) == [
        {"role": "assistant", "content": "¡Hola!"},
        {"role": "user", "content": "(nota de voz) Quiero reservar para el sábado"},
    ]


def test_new_contact_voice_note_enables_ai(gemini_key):
    from app.application.ai.ai_auto_enable_service import maybe_auto_enable_ai_for_inbound
    from app.domain.entities import Conversation, Tenant

    tenant = Tenant(business_name="Motel Luna", slug="x", ai_global_enabled=True)
    conv = Conversation(contact_phone="+573001112233", imported_legacy=False, ai_active=False)
    assert maybe_auto_enable_ai_for_inbound(tenant=tenant, conversation=conv, body="[audio]") is True
    assert conv.ai_active is True


def _setup(db):
    from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus

    tenant = Tenant(
        business_name="Motel Luna",
        slug=f"luna-{uuid.uuid4().hex[:8]}",
        whatsapp_status=WhatsAppStatus.CONNECTED.value,
        ai_global_enabled=True,
    )
    db.add(tenant)
    db.flush()
    db.add(WhatsAppSession(tenant_id=tenant.id, instance_name=f"inst_{uuid.uuid4().hex[:8]}", status="connected"))
    conversation = Conversation(
        tenant_id=tenant.id,
        contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
        contact_name="Cliente",
        ai_active=True,
        mode="auto",
    )
    db.add(conversation)
    db.flush()
    voice = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction="in",
        source="contact",
        body="[audio]",
        status="received",
        evolution_message_id=f"EVO{uuid.uuid4().hex[:10]}",
    )
    db.add(voice)
    db.flush()
    return tenant, conversation, voice


@requires_db
def test_voice_note_is_transcribed_once(monkeypatch, gemini_key):
    from app.application.ai import ai_transcription_service as svc
    from app.infrastructure.persistence.database import SessionLocal

    calls: list[str] = []
    events: list[str] = []
    monkeypatch.setattr(
        svc, "fetch_message_media", lambda *_a, **_kw: {"base64": "QUJD", "mimetype": "audio/ogg", "media_type": "audio"}
    )
    monkeypatch.setattr(svc.gemini_client, "transcribe_audio", lambda b64, mime: calls.append(b64) or "¿Precio de la noche?")
    monkeypatch.setattr(svc, "publish_message_event", lambda *_a, event_type, **_kw: events.append(event_type))

    with SessionLocal() as db:
        tenant, conversation, voice = _setup(db)
        assert svc.ensure_transcript(db, tenant=tenant, conversation=conversation, message=voice) == "¿Precio de la noche?"
        assert svc.ensure_transcript(db, tenant=tenant, conversation=conversation, message=voice) == "¿Precio de la noche?"
        assert calls == ["QUJD"]
        assert events == ["message.updated"]
        db.rollback()


@requires_db
def test_voice_note_gets_scheduled_only_with_gemini(monkeypatch):
    from app.application.ai import ai_queue_service, ai_service
    from app.config import settings
    from app.infrastructure.persistence.database import SessionLocal

    enqueued: list[uuid.UUID] = []
    monkeypatch.setattr(ai_queue_service, "enqueue_ai_reply_ids", lambda **kw: enqueued.append(kw["message_id"]))

    with SessionLocal() as db:
        tenant, conversation, voice = _setup(db)

        monkeypatch.setattr(settings, "gemini_api_key", "")
        assert ai_service.maybe_schedule_ai_for_conversation(db, tenant_id=tenant.id, conversation_id=conversation.id) is False

        monkeypatch.setattr(settings, "gemini_api_key", "test-key")
        assert ai_service.maybe_schedule_ai_for_conversation(db, tenant_id=tenant.id, conversation_id=conversation.id) is True
        assert enqueued == [voice.id]
        db.rollback()

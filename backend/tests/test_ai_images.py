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


@pytest.fixture
def gemini_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    return settings


def _msg(body: str, transcript: str | None = None, direction: str = "in"):
    return SimpleNamespace(direction=direction, body=body, transcript=transcript)


def test_describe_image_uses_image_model_and_mime(monkeypatch, gemini_key):
    from app.infrastructure.ai import gemini_client

    seen: dict = {}

    def fake_post(self, url, json=None, headers=None):
        seen.update(url=url, json=json)
        return _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "Habitación con jacuzzi."}]}}]})

    monkeypatch.setattr(gemini_key, "gemini_image_model", "modelo-imagen")
    monkeypatch.setattr(httpx.Client, "post", fake_post)

    assert gemini_client.describe_image("QUJD", "image/jpeg") == "Habitación con jacuzzi."
    assert seen["url"].endswith("/models/modelo-imagen:generateContent")
    assert seen["json"]["contents"][0]["parts"][1]["inline_data"] == {"mime_type": "image/jpeg", "data": "QUJD"}


def test_captioned_photo_keeps_media_marker():
    from app.application.messaging.message_service import _extract_message_body

    assert _extract_message_body({"imageMessage": {"caption": "¿Tienen esta?"}}) == "[image]\n¿Tienen esta?"
    assert _extract_message_body({"imageMessage": {"mimetype": "image/jpeg"}}) == "[image]"
    assert _extract_message_body({"videoMessage": {"caption": "mira"}}) == "[video]\nmira"
    assert _extract_message_body({"stickerMessage": {}}) == "[sticker]"


def test_text_for_ai_combines_description_and_caption():
    from app.application.ai.ai_service import is_respondable_text
    from app.application.messaging.message_service import text_for_ai

    full = _msg("[image]\n¿Tienen esta?", "Habitación con jacuzzi y luces rojas.")
    assert text_for_ai(full) == "(imagen: Habitación con jacuzzi y luces rojas.) ¿Tienen esta?"

    caption_only = _msg("[image]\n¿Tienen esta?")
    assert text_for_ai(caption_only) == "¿Tienen esta?"
    assert is_respondable_text(text_for_ai(caption_only)) is True

    bare = _msg("[image]")
    assert text_for_ai(bare) == "[image]"
    assert is_respondable_text(text_for_ai(bare)) is False

    assert text_for_ai(_msg("[image]", "")) == "[image]"


def test_history_shows_image_description():
    from app.application.ai.ai_conversation_service import format_history

    rows = [_msg("[image]", "Menú con hamburguesa a $25.000."), _msg("[Imagen]\nNuestra carta", direction="out")]
    assert format_history(rows) == [
        {"role": "user", "content": "(imagen: Menú con hamburguesa a $25.000.)"},
        {"role": "assistant", "content": "[Imagen]\nNuestra carta"},
    ]


def test_payment_receipt_counts_as_purchase_intent():
    from app.application.ai.ai_qualify_service import detect_purchase_intent
    from app.application.messaging.message_service import text_for_ai

    receipt = _msg("[image]", "Comprobante de pago: Nequi, $80.000, 1 oct 2026, ref M123.")
    assert detect_purchase_intent(text_for_ai(receipt)) is True
    assert detect_purchase_intent(text_for_ai(_msg("[image]", "Foto de una playa."))) is False


def test_reply_prompt_forbids_confirming_payments(monkeypatch):
    from app.application.ai import ai_conversation_service as svc

    seen: dict = {}
    monkeypatch.setattr(svc, "chat_completion", lambda messages, **_kw: seen.update(messages=messages) or "Ya lo revisamos")
    svc._complete_reply(system="Base", history=[], shortcuts=[])
    system = seen["messages"][0]["content"]
    assert system.startswith("Base")
    assert "Nunca confirmes que un pago fue recibido" in system


def test_only_photos_get_described(gemini_key):
    from app.application.ai.ai_transcription_service import can_transcribe

    assert can_transcribe(_msg("[image]\nhola")) is True
    assert can_transcribe(_msg("[sticker]")) is False
    assert can_transcribe(_msg("[image]", "ya descrita")) is False


def test_new_contact_photo_enables_ai(gemini_key):
    from app.application.ai.ai_auto_enable_service import maybe_auto_enable_ai_for_inbound
    from app.domain.entities import Conversation, Tenant

    tenant = Tenant(business_name="Motel Luna", slug="x", ai_global_enabled=True)
    conv = Conversation(contact_phone="+573001112233", imported_legacy=False, ai_active=False)
    assert maybe_auto_enable_ai_for_inbound(tenant=tenant, conversation=conv, body="[image]") is True
    assert conv.ai_active is True


@requires_db
def test_photo_is_described_once(monkeypatch, gemini_key):
    from app.application.ai import ai_transcription_service as svc
    from app.domain.entities import Conversation, Message, Tenant
    from app.infrastructure.persistence.database import SessionLocal

    calls: list[str] = []
    monkeypatch.setattr(
        svc, "fetch_message_media", lambda *_a, **_kw: {"base64": "QUJD", "mimetype": "image/jpeg", "media_type": "image"}
    )
    monkeypatch.setattr(svc.gemini_client, "describe_image", lambda b64, mime: calls.append(mime) or "Habitación doble.")
    monkeypatch.setattr(svc.gemini_client, "transcribe_audio", lambda *_a: pytest.fail("no es audio"))
    monkeypatch.setattr(svc, "publish_message_event", lambda *_a, **_kw: None)

    with SessionLocal() as db:
        tenant = Tenant(business_name="Motel Luna", slug=f"luna-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        db.flush()
        conversation = Conversation(tenant_id=tenant.id, contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}")
        db.add(conversation)
        db.flush()
        photo = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            direction="in",
            source="contact",
            body="[image]\n¿Esta está libre?",
            status="received",
            evolution_message_id=f"EVO{uuid.uuid4().hex[:10]}",
        )
        db.add(photo)
        db.flush()

        assert svc.ensure_transcript(db, tenant=tenant, conversation=conversation, message=photo) == "Habitación doble."
        assert svc.ensure_transcript(db, tenant=tenant, conversation=conversation, message=photo) == "Habitación doble."
        assert calls == ["image/jpeg"]
        db.rollback()

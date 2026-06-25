from __future__ import annotations

import uuid
from unittest.mock import patch

from app.domain.entities.enums import ConversationMode, ConversationStatus
from app.application.ai.ai_classifier_service import _heuristic_classify, classify_inbound_message
from app.application.ai.ai_service import is_respondable_text, should_ai_respond
from tests.conftest import requires_db


class _FakeTenant:
    ai_global_enabled = True


class _FakeConversation:
    ai_active = True
    mode = ConversationMode.AUTO.value
    status = ConversationStatus.ACTIVE.value


def test_should_ai_respond_when_manual_mode():
    conv = _FakeConversation()
    conv.mode = ConversationMode.MANUAL.value
    assert should_ai_respond(_FakeTenant(), conv) is False


def test_should_ai_respond_when_ai_inactive():
    conv = _FakeConversation()
    conv.ai_active = False
    assert should_ai_respond(_FakeTenant(), conv) is False


def test_should_ai_respond_when_global_off():
    tenant = _FakeTenant()
    tenant.ai_global_enabled = False
    assert should_ai_respond(tenant, _FakeConversation()) is False


def test_should_ai_respond_ok():
    assert should_ai_respond(_FakeTenant(), _FakeConversation()) is True


def test_is_respondable_text_skips_media():
    assert is_respondable_text("[image]") is False
    assert is_respondable_text("Hola, me interesa") is True


def test_heuristic_opt_out():
    result = _heuristic_classify("Por favor no me escribas más")
    assert result["category"] == "opt_out"


def test_heuristic_ruido():
    result = _heuristic_classify("ok")
    assert result["category"] == "ruido"


def test_heuristic_hola_is_interesado():
    result = _heuristic_classify("hola")
    assert result["category"] == "interesado"


@patch("app.application.ai.ai_classifier_service.chat_completion")
def test_classifier_overrides_informal_greeting_from_ruido(mock_chat):
    mock_chat.return_value = '{"category":"ruido","reason":"saludo informal"}'
    result = classify_inbound_message("Que haces hermanito")
    assert result["category"] == "interesado"


@patch("app.application.ai.ai_classifier_service.chat_completion")
def test_classifier_keeps_trivial_ok_as_ruido(mock_chat):
    mock_chat.return_value = '{"category":"ruido","reason":"ack"}'
    result = classify_inbound_message("ok")
    assert result["category"] == "ruido"


def test_resolve_model_blocks_expensive():
    from app.infrastructure.ai.deepseek_client import resolve_model

    assert resolve_model("deepseek-reasoner") == "deepseek-chat"
    assert resolve_model("deepseek-v4-pro") == "deepseek-chat"
    assert resolve_model("deepseek-chat") == "deepseek-chat"


@patch("app.application.ai.ai_classifier_service.chat_completion")
def test_classify_parses_json(mock_chat):
    mock_chat.return_value = '{"category":"duda","reason":"pregunta precio"}'
    result = classify_inbound_message("¿Cuánto cuesta?")
    assert result["category"] == "duda"


@requires_db
@patch("app.application.ai.ai_service.send_text_message")
@patch("app.application.ai.ai_service.generate_reply")
@patch("app.application.ai.ai_service.classify_inbound_message")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_process_ai_reply_sends_message(
    _sleep,
    mock_classify,
    mock_generate,
    mock_send,
):
    from app.infrastructure.persistence.database import SessionLocal
    from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
    from app.domain.entities.enums import MessageDirection, MessageSource, WhatsAppStatus
    from app.application.ai.ai_service import process_ai_reply

    mock_classify.return_value = {"category": "interesado", "reason": "test"}
    mock_generate.return_value = "¡Hola! Claro que sí, cuéntame."

    with SessionLocal() as db:
        tenant = Tenant(
            business_name="Test Biz",
            slug=f"t-{uuid.uuid4().hex[:8]}",
            ai_global_enabled=True,
        )
        db.add(tenant)
        db.flush()

        session = WhatsAppSession(
            tenant_id=tenant.id,
            instance_name=f"inst_{uuid.uuid4().hex[:8]}",
            status=WhatsAppStatus.CONNECTED.value,
        )
        db.add(session)

        conv = Conversation(
            tenant_id=tenant.id,
            contact_phone="+573001112233",
            contact_name="Juan",
            ai_active=True,
            mode=ConversationMode.AUTO.value,
            bait_sent=True,
            whatsapp_connection_id=uuid.uuid4(),
        )
        db.add(conv)
        db.flush()

        msg = Message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            direction=MessageDirection.IN.value,
            source=MessageSource.CONTACT.value,
            body="Me interesa saber más",
            status="received",
        )
        db.add(msg)
        db.commit()

        with patch("app.application.ai.ai_service.is_configured", return_value=True):
            ok = process_ai_reply(
                db,
                tenant_id=tenant.id,
                conversation_id=conv.id,
                message_id=msg.id,
            )
        assert ok is True
        mock_send.assert_called_once()

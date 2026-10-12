"""La IA no habla por el dueño: ni contesta lo que él ya contestó, ni se mete en chats que él lleva."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.application.ai.ai_shortcut_service import AiGeneratedReply
from tests.conftest import requires_db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add(db, conv, *, direction: str, source: str, body: str, at: datetime):
    from app.domain.entities import Message

    row = Message(
        tenant_id=conv.tenant_id,
        conversation_id=conv.id,
        direction=direction,
        source=source,
        body=body,
        status="sent" if direction == "out" else "received",
        created_at=at,
    )
    db.add(row)
    db.flush()
    return row


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_what_the_owner_already_answered_is_not_answered_again(_sleep, mock_generate, mock_send):
    from app.application.ai.ai_service import maybe_schedule_ai_for_conversation
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run, _seed

    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="6:30?")
        msg.created_at = _now() - timedelta(minutes=40)
        _add(db, conv, direction="out", source="agent", body="listo", at=_now() - timedelta(minutes=39))
        db.commit()
        assert maybe_schedule_ai_for_conversation(db, tenant_id=tenant.id, conversation_id=conv.id) is False
        assert _run(db, tenant, conv, msg) is True

    mock_generate.assert_not_called()
    mock_send.assert_not_called()


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_steps_aside_in_a_chat_the_owner_is_carrying(_sleep, mock_generate, mock_send):
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run, _seed

    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="Literal todos los demás exclusivos")
        msg.created_at = _now()
        _add(db, conv, direction="out", source="agent", body="jajaja mire este", at=_now() - timedelta(minutes=3))
        db.commit()
        assert _run(db, tenant, conv, msg) is True

    mock_generate.assert_not_called()
    mock_send.assert_not_called()
    assert conv.ai_active is False and conv.mode == "manual"


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_echo_of_the_ai_message_is_not_the_owner(_sleep, mock_generate, mock_send):
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run, _seed

    mock_generate.return_value = AiGeneratedReply(message="Tenemos la suite a $120.000 😊")
    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="¿Y cuánto vale la suite?")
        msg.created_at = _now()
        said = "¡Hola! Bienvenido, ¿en qué te ayudo?"
        _add(db, conv, direction="out", source="bot", body=said, at=_now() - timedelta(minutes=2))
        _add(db, conv, direction="out", source="agent", body=said, at=_now() - timedelta(minutes=2))
        db.commit()
        assert _run(db, tenant, conv, msg) is True

    mock_send.assert_called_once()
    assert conv.ai_active is True and conv.mode == "auto"


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_owner_turned_the_ai_on_after_writing(_sleep, mock_generate, mock_send):
    from app.application.ai.ai_service import mark_ai_enabled_by_owner
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run, _seed

    mock_generate.return_value = AiGeneratedReply(message="Tenemos la suite a $120.000 😊")
    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="¿Y cuánto vale la suite?")
        msg.created_at = _now()
        _add(db, conv, direction="out", source="agent", body="Hola, ya te atienden", at=_now() - timedelta(minutes=5))
        db.commit()
        mark_ai_enabled_by_owner(conv.id)
        assert _run(db, tenant, conv, msg) is True

    mock_send.assert_called_once()
    assert conv.ai_active is True


@requires_db
@patch("app.application.ai.ai_auto_enable_service.classify_inbound_message")
def test_ai_does_not_switch_itself_on_where_the_owner_writes(mock_classify):
    from app.application.ai.ai_auto_enable_service import maybe_auto_enable_ai_for_inbound
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _seed

    mock_classify.return_value = {"category": "duda"}
    with SessionLocal() as db:
        tenant, conv, _ = _seed(db, body="¿Qué más primo, cuánto le costó eso?")
        conv.ai_active = False
        conv.bait_sent = False
        _add(db, conv, direction="out", source="agent", body="Quiubo primo", at=_now() - timedelta(days=2))
        db.commit()
        assert maybe_auto_enable_ai_for_inbound(
            tenant=tenant, conversation=conv, body="¿Qué más primo, cuánto le costó eso?", db=db
        ) is False

    mock_classify.assert_not_called()
    assert conv.ai_active is False

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
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_stays_out_of_the_owners_personal_chats(_sleep, mock_generate, mock_send, monkeypatch):
    """La novia del dueño escribe 20 min después de que él le habló: la IA no le contesta «jajaja»."""
    from app.application.ai import ai_service
    from app.application.ai.ai_service import mark_ai_enabled_by_owner
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run, _seed

    seen: list = []
    monkeypatch.setattr(ai_service, "is_personal_chat", lambda lines, business_name="": seen.append(lines) or True)
    mock_generate.return_value = AiGeneratedReply(message="Jajajajaja")
    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="Un poco interactiva ando")
        msg.created_at = _now()
        _add(db, conv, direction="out", source="agent", body="Dale amor nos vemos", at=_now() - timedelta(minutes=30))
        _add(db, conv, direction="out", source="agent", body="Del oficio que haces", at=_now() - timedelta(minutes=21))
        db.commit()
        # Tocó «IA activa» sin querer: lo que él escribió antes ya no la aparta.
        mark_ai_enabled_by_owner(conv.id)
        assert _run(db, tenant, conv, msg) is True
        assert conv.ai_active is False and conv.mode == "manual"

        # Si la vuelve a prender a propósito, manda él y no se le pregunta otra vez a la IA.
        conv.ai_active, conv.mode = True, "auto"
        db.commit()
        mark_ai_enabled_by_owner(conv.id)
        assert _run(db, tenant, conv, msg) is True

    assert len(seen) == 1
    assert ("agent", "Dale amor nos vemos") in seen[0] and ("contact", "Un poco interactiva ando") in seen[0]
    mock_send.assert_called_once()


def test_personal_chat_check_reads_who_said_what(monkeypatch):
    from app.application.ai import ai_classifier_service as svc

    sent: list = []
    monkeypatch.setattr(svc, "is_configured", lambda: True)
    monkeypatch.setattr(svc, "chat_completion", lambda messages, **kw: sent.append(messages) or '{"personal": true}')
    assert svc.is_personal_chat([("agent", "Dale amor"), ("contact", "Un poco interactiva ando")], business_name="Rino")
    assert sent[0][1]["content"] == "Dueño: Dale amor\nContacto: Un poco interactiva ando"
    assert "«Rino»" in sent[0][0]["content"]

    monkeypatch.setattr(svc, "chat_completion", lambda messages, **kw: "no sé")
    assert svc.is_personal_chat([("contact", "hola amor, ¿cuánto vale la torta?")]) is False


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


def _meanwhile(change):
    """Lo que pasa en el panel mientras la IA redacta: otra sesión, otro commit."""
    def _generate(*_args, **_kwargs):
        from app.infrastructure.persistence.database import SessionLocal

        with SessionLocal() as other:
            change(other)
            other.commit()
        return AiGeneratedReply(message="Claro, te cuento los precios 😊")

    return _generate


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_owner_turns_ai_off_while_it_is_drafting(_sleep, mock_generate, mock_send):
    from app.domain.entities import Tenant
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run, _seed

    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="¿Cuánto valen?")
        msg.created_at = _now()
        db.commit()
        tenant_id = tenant.id
        mock_generate.side_effect = _meanwhile(
            lambda other: other.query(Tenant).filter(Tenant.id == tenant_id).update({"ai_global_enabled": False})
        )
        _run(db, tenant, conv, msg)

    mock_generate.assert_called_once()
    mock_send.assert_not_called()


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_owner_answers_while_ai_is_drafting(_sleep, mock_generate, mock_send):
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run, _seed

    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="¿Cuánto valen?")
        msg.created_at = _now() - timedelta(seconds=30)
        db.commit()

        def _owner_writes(other):
            from app.domain.entities import Conversation

            _add(other, other.get(Conversation, conv.id), direction="out", source="agent", body="Ya te digo", at=_now())

        mock_generate.side_effect = _meanwhile(_owner_writes)
        _run(db, tenant, conv, msg)

    mock_generate.assert_called_once()
    mock_send.assert_not_called()
    assert conv.ai_active is False and conv.mode == "manual"

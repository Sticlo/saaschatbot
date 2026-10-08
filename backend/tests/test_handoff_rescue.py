"""Chat pasado a una persona que nadie atiende: la IA lo retoma en vez de dejar al cliente colgado."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.application.ai.ai_shortcut_service import AiGeneratedReply
from tests.conftest import requires_db

OWNER = [{"name": "Dueña", "phone": "+573001234567", "scope": "all"}]


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


def _handed_off_chat(db, *, client_wrote_again: bool):
    """Como el chat de Lorena: la IA lo pasó a una persona hace 6 min y nadie respondió."""
    from app.application.ai.ai_service import _awaiting_key
    from app.infrastructure.cache.redis_client import get_redis
    from tests.test_sales_stage_and_booking import _seed

    tenant, conv, first = _seed(
        db, body="Me gustaría un corte de niño para mi hijo", alert_recipients=OWNER
    )
    handed_at = _now() - timedelta(minutes=6)
    first.created_at = handed_at - timedelta(seconds=5)
    _add(db, conv, direction="out", source="bot", body="Le paso tu solicitud al equipo 🙌", at=handed_at)
    latest = first
    if client_wrote_again:
        latest = _add(
            db, conv, direction="in", source="contact", body="Dale me avisas",
            at=handed_at + timedelta(minutes=1),
        )
    conv.ai_active = False
    conv.mode = "manual"
    db.commit()
    get_redis().set(_awaiting_key(conv.id), str(handed_at.timestamp()), ex=3600)
    return tenant, conv, latest, handed_at


def _rescue(db, tenant, conv, msg):
    from app.application.ai.ai_service import rescue_unanswered_handoff

    ok = rescue_unanswered_handoff(db, tenant_id=tenant.id, conversation_id=conv.id, message_id=msg.id)
    db.commit()
    db.refresh(conv)
    return ok


@requires_db
@patch("app.application.ai.ai_queue_service.schedule_ai_retry")
@patch("app.application.conversations.interest_alert_service.send_alert")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_handing_the_chat_to_a_person_schedules_a_rescue(_sleep, mock_generate, _send, _alert, mock_schedule):
    from app.application.ai.ai_service import HANDOFF_RESCUE_SECONDS, _awaiting_human_since
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run, _seed

    mock_generate.return_value = AiGeneratedReply(message="¡De una! Dame un momentico 🙌", stage="cierre")
    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="Me llevo los blancos en talla 38", alert_recipients=OWNER)
        assert _run(db, tenant, conv, msg) is True
        expected = (tenant.id, conv.id, msg.id)

    assert conv.mode == "manual"
    assert _awaiting_human_since(conv.id) is not None
    args, kwargs = mock_schedule.call_args
    assert args == expected
    assert kwargs == {"delay_seconds": HANDOFF_RESCUE_SECONDS, "kind": "rescue"}


@requires_db
@patch("app.application.ai.ai_queue_service.schedule_ai_retry")
@patch("app.application.conversations.interest_alert_service.send_alert", return_value=True)
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_a_promise_to_get_back_hands_the_chat_to_a_person(_sleep, mock_generate, _send, mock_alert, mock_schedule):
    from app.application.ai.ai_service import _awaiting_human_since
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run, _seed

    mock_generate.return_value = AiGeneratedReply(message="Déjame revisar si lo hay en talla 38 y ya te confirmo 🙌")
    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="¿Lo tienes en talla 38?", alert_recipients=OWNER)
        assert _run(db, tenant, conv, msg) is True

    assert conv.mode == "manual" and conv.ai_active is False
    assert _awaiting_human_since(conv.id) is not None
    mock_alert.assert_called()
    assert mock_schedule.call_args.kwargs["kind"] == "rescue"


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert", return_value=True)
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_nobody_answers_the_client_so_the_ai_takes_the_chat_back(_sleep, mock_generate, mock_send, mock_alert):
    from app.application.ai.ai_service import _awaiting_human_since
    from app.infrastructure.persistence.database import SessionLocal

    mock_generate.return_value = AiGeneratedReply(
        message="¡Claro! ¿Qué día y a qué hora te queda bien para el corte de tu hijo?"
    )
    with SessionLocal() as db:
        tenant, conv, latest, _ = _handed_off_chat(db, client_wrote_again=True)
        assert _rescue(db, tenant, conv, latest) is True

    assert conv.ai_active is True and conv.mode == "auto"
    assert mock_send.call_args.kwargs["reply"].message.startswith("¡Claro! ¿Qué día")
    assert "la IA retomó el chat" in mock_alert.call_args.args[2]
    assert _awaiting_human_since(conv.id) is None


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert", return_value=True)
@patch("app.application.ai.ai_service.generate_qualify_reply")
def test_a_person_already_answered_so_the_ai_stays_out(mock_generate, mock_alert):
    from app.application.ai.ai_service import _awaiting_human_since
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, latest, handed_at = _handed_off_chat(db, client_wrote_again=True)
        _add(db, conv, direction="out", source="agent", body="Hola, ya te agendo 🙌", at=_now())
        db.commit()
        assert _rescue(db, tenant, conv, latest) is True

    assert conv.mode == "manual" and conv.ai_active is False
    mock_generate.assert_not_called()
    mock_alert.assert_not_called()
    assert _awaiting_human_since(conv.id) is None


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert", return_value=True)
@patch("app.application.ai.ai_service.generate_qualify_reply")
def test_client_did_not_write_again_so_only_the_owner_is_reminded(mock_generate, mock_alert):
    from app.application.ai.ai_service import _awaiting_human_since
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, latest, _ = _handed_off_chat(db, client_wrote_again=False)
        assert _rescue(db, tenant, conv, latest) is True

    assert conv.mode == "manual"
    mock_generate.assert_not_called()
    assert "lleva 5 min esperando" in mock_alert.call_args.args[2]
    assert _awaiting_human_since(conv.id) is not None  # si vuelve a escribir, la IA lo atiende


@requires_db
@patch("app.application.ai.ai_queue_service.schedule_ai_retry")
def test_client_writing_into_a_waiting_chat_schedules_the_rescue(mock_schedule):
    from app.application.ai.ai_queue_service import enqueue_ai_reply_ids
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, latest, _ = _handed_off_chat(db, client_wrote_again=True)
        assert enqueue_ai_reply_ids(tenant.id, conv.id, latest.id, db=db) is False
        expected = (tenant.id, conv.id, latest.id)

    assert mock_schedule.call_args.args == expected
    assert mock_schedule.call_args.kwargs["kind"] == "rescue"


@requires_db
@patch("app.application.ai.ai_queue_service.schedule_ai_retry")
def test_owner_decision_in_the_panel_cancels_the_rescue(mock_schedule):
    from app.application.ai.ai_queue_service import enqueue_ai_reply_ids
    from app.application.ai.ai_service import clear_awaiting_human
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, latest, _ = _handed_off_chat(db, client_wrote_again=True)
        clear_awaiting_human(conv.id)
        assert enqueue_ai_reply_ids(tenant.id, conv.id, latest.id, db=db) is False

    mock_schedule.assert_not_called()


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert", return_value=True)
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_after_a_rescue_the_ai_does_not_drop_the_chat_again(_sleep, mock_generate, _send, _alert):
    from app.application.ai.ai_service import _rescued_key
    from app.infrastructure.cache.redis_client import get_redis
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run, _seed

    mock_generate.return_value = AiGeneratedReply(message="¡De una! Te cuento cómo pagar 🙌", stage="cierre")
    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="Listo, ¿cómo pago?", alert_recipients=OWNER)
        get_redis().set(_rescued_key(conv.id), "1", ex=60)
        assert _run(db, tenant, conv, msg) is True

    assert conv.mode == "auto" and conv.ai_active is True
    assert conv.interest_status == "interested"

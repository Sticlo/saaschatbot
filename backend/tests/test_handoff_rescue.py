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
@patch("app.application.conversations.interest_alert_service.send_alert", return_value=True)
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ready_to_buy_alerts_the_owner_but_the_ai_keeps_attending(
    _sleep, mock_generate, _send, mock_alert, mock_schedule
):
    from app.application.ai.ai_service import _awaiting_human_since
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run, _seed

    # Como el chat de Juan: la IA pregunta «¿qué día tienes en mente?» y el cliente responde.
    mock_generate.return_value = AiGeneratedReply(
        message="¡Qué bueno! Te confirmo los detalles con el equipo. ¿Qué día tienes en mente?", stage="cierre"
    )
    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="Me siento interesado, cómo puedo contratarte", alert_recipients=OWNER)
        assert _run(db, tenant, conv, msg) is True

    assert conv.mode == "auto" and conv.ai_active is True
    assert conv.interest_status == "interested"
    assert _awaiting_human_since(conv.id) is not None
    mock_alert.assert_called()
    # Al cliente se le dijo que el negocio le confirma: el dueño recibe recordatorios.
    assert [c.kwargs["delay_seconds"] for c in mock_schedule.call_args_list] == [300, 1800]
    assert all(c.kwargs["kind"] == "rescue" for c in mock_schedule.call_args_list)


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert", return_value=True)
@patch("app.application.ai.ai_service.generate_qualify_reply")
def test_owner_who_has_not_confirmed_gets_reminded(mock_generate, mock_alert):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, latest = _alerted_chat(db, owner_wrote=False)
        assert _rescue(db, tenant, conv, latest) is True

    mock_generate.assert_not_called()
    assert conv.mode == "auto" and conv.ai_active is True
    assert "esperando que le confirmes" in mock_alert.call_args.args[2]


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert", return_value=True)
def test_owner_who_already_wrote_is_not_reminded(mock_alert):
    from app.application.ai.ai_service import _awaiting_human_since
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, latest = _alerted_chat(db, owner_wrote=True)
        assert _rescue(db, tenant, conv, latest) is True

    mock_alert.assert_not_called()
    assert _awaiting_human_since(conv.id) is None


def _alerted_chat(db, *, owner_wrote: bool):
    """El dueño recibió el aviso de venta hace 2 min y el cliente siguió escribiendo."""
    from app.application.ai.ai_service import _awaiting_key
    from app.infrastructure.cache.redis_client import get_redis
    from tests.test_sales_stage_and_booking import _seed

    tenant, conv, first = _seed(db, body="Me siento interesado", alert_recipients=OWNER)
    alerted_at = _now() - timedelta(minutes=2)
    first.created_at = alerted_at - timedelta(seconds=10)
    _add(db, conv, direction="out", source="bot", body="¿Qué día tienes en mente?", at=alerted_at)
    if owner_wrote:
        _add(db, conv, direction="out", source="agent", body="Hola Juan, soy Jair 👋", at=alerted_at + timedelta(minutes=1))
    latest = _add(db, conv, direction="in", source="contact", body="Se puede?", at=_now())
    db.commit()
    get_redis().set(_awaiting_key(conv.id), str(alerted_at.timestamp()), ex=3600)
    return tenant, conv, latest


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_client_keeps_writing_after_the_alert_and_gets_an_answer(_sleep, mock_generate, mock_send):
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    mock_generate.return_value = AiGeneratedReply(message="¡Claro que se puede! ¿Para qué red lo quieres?")
    with SessionLocal() as db:
        tenant, conv, latest = _alerted_chat(db, owner_wrote=False)
        assert _run(db, tenant, conv, latest) is True

    mock_send.assert_called_once()
    assert conv.mode == "auto" and conv.ai_active is True


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_owner_writes_after_the_alert_so_the_ai_steps_aside(_sleep, mock_generate, mock_send):
    from app.application.ai.ai_service import _awaiting_human_since
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    with SessionLocal() as db:
        tenant, conv, latest = _alerted_chat(db, owner_wrote=True)
        assert _run(db, tenant, conv, latest) is True

    mock_generate.assert_not_called()
    mock_send.assert_not_called()
    assert conv.mode == "manual" and conv.ai_active is False
    # Si el dueño se metió y luego no responde, el rescate sigue armado.
    assert _awaiting_human_since(conv.id) is not None


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
        conv.ai_set_by_agent = True  # la apagó a propósito en el panel
        clear_awaiting_human(conv.id)
        db.commit()
        assert enqueue_ai_reply_ids(tenant.id, conv.id, latest.id, db=db) is False

    mock_schedule.assert_not_called()


@requires_db
@patch("app.application.ai.ai_queue_service.schedule_ai_retry")
def test_client_writing_again_after_phone_takeover_rearms_rescue(mock_schedule):
    """Celular contestó, se fue, cliente pide fotos: a los 5 min la IA retoma."""
    from app.application.ai.ai_queue_service import enqueue_ai_reply_ids
    from app.application.ai.ai_service import _awaiting_human_since, clear_awaiting_human
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, latest, _ = _handed_off_chat(db, client_wrote_again=True)
        conv.ai_set_by_agent = False
        clear_awaiting_human(conv.id)
        db.commit()
        assert enqueue_ai_reply_ids(tenant.id, conv.id, latest.id, db=db) is False
        assert _awaiting_human_since(conv.id) is not None

    assert mock_schedule.call_args.kwargs["kind"] == "rescue"


@requires_db
@patch("app.application.ai.ai_queue_service.schedule_ai_retry")
def test_owner_takeover_schedules_rescue_so_client_is_not_left_hanging(mock_schedule):
    """Si el celular se mete al chat y luego nadie responde, la IA retoma a los 5 min."""
    from app.application.ai.ai_service import _awaiting_human_since, _owner_took_over
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _seed

    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="¿Tienen habitación para mañana?")
        conv.ai_active = True
        conv.mode = "auto"
        bot_at = _now() - timedelta(minutes=2)
        _add(db, conv, direction="out", source="bot", body="¡Claro! ¿Para cuántas personas?", at=bot_at)
        _add(
            db, conv, direction="out", source="agent",
            body="Listo, yo te atiendo desde acá",
            at=bot_at + timedelta(seconds=30),
        )
        db.commit()
        assert _owner_took_over(db, tenant=tenant, conversation=conv) is True
        assert conv.ai_active is False and conv.mode == "manual"
        assert _awaiting_human_since(conv.id) is not None
        db.commit()

    assert mock_schedule.call_args.kwargs["kind"] == "rescue"
    assert mock_schedule.call_args.kwargs["delay_seconds"] == 300


@requires_db
def test_short_phone_echo_does_not_count_as_owner_takeover():
    """Un «H» del celular justo después de la IA no aparta el chat."""
    from app.application.ai.ai_service import _owner_took_over
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _seed

    with SessionLocal() as db:
        tenant, conv, _ = _seed(db, body="H")
        conv.ai_active = True
        conv.mode = "auto"
        bot_at = _now() - timedelta(seconds=5)
        _add(db, conv, direction="out", source="bot", body="¡Hola! Bienvenida al hotel 🏨", at=bot_at)
        _add(db, conv, direction="out", source="agent", body="H", at=bot_at + timedelta(seconds=1))
        db.commit()
        assert _owner_took_over(db, tenant=tenant, conversation=conv) is False
        assert conv.ai_active is True and conv.mode == "auto"


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert", return_value=True)
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_rescue_after_owner_phone_reply_does_not_loop_back_to_manual(
    _sleep, mock_generate, mock_send, _alert
):
    """El celular mandó el recibo y se fue: al rescatar, ese mensaje viejo no puede volver a apagar la IA."""
    from app.application.ai.ai_service import _awaiting_human_since, _owner_took_over
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _seed

    mock_generate.return_value = AiGeneratedReply(
        message="Claro, la dirección es Carrera 50. ¿Algo más?", stage="cierre"
    )
    mock_send.return_value = True

    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="¿Me pasas la dirección?")
        conv.ai_active = True
        conv.mode = "auto"
        bot_at = _now() - timedelta(minutes=20)
        _add(db, conv, direction="out", source="bot", body="¡Claro! Te confirmo en un momento", at=bot_at)
        _add(
            db, conv, direction="out", source="agent",
            body="¡Listo! Te compartimos el recibo de tu reserva",
            at=bot_at + timedelta(minutes=2),
        )
        pending = _add(
            db, conv, direction="in", source="contact", body="Y la dirección porfa",
            at=bot_at + timedelta(minutes=5),
        )
        db.commit()
        assert _owner_took_over(db, tenant=tenant, conversation=conv) is True
        db.commit()

        assert _rescue(db, tenant, conv, pending) is True
        assert conv.ai_active is True and conv.mode == "auto"
        # El mensaje viejo del celular ya no la aparta; si quedó en awaiting es por el
        # cierre («el negocio te confirma»), no por otro takeover.
        assert mock_send.called
        assert _owner_took_over(db, tenant=tenant, conversation=conv) is False


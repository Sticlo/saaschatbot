"""Cuidar el número: la IA lee antes de responder, no manda ráfagas ni mensajes kilométricos,
y cada corte o sesión abierta en otro lado queda registrado (y el dueño se entera)."""
from __future__ import annotations

import uuid
from unittest.mock import patch

from app.application.whatsapp.whatsapp_service import split_for_whatsapp
from app.application.whatsapp.whatsapp_status import classify_disconnect
from tests.conftest import requires_db
from tests.test_message_replies import chat  # noqa: F401
from tests.test_whatsapp_health import _linked_session, _register

SEND = "app.application.whatsapp.whatsapp_service.gateway_send_text"


def _sent(*_args, **_kwargs) -> dict:
    return {"key": {"id": f"WA-{uuid.uuid4().hex[:10]}"}}


READ = "app.application.whatsapp.whatsapp_service.gateway_mark_read"
LONG = (
    ("Te cuento del paquete baby shower: incluye arco de globos, mesa principal y letrero. " * 5).strip()
    + "\n\n"
    + ("Para separar la fecha pedimos el 50% de anticipo y el resto el día del evento. " * 5).strip()
)


def test_long_text_is_split_by_paragraphs_without_losing_words():
    assert split_for_whatsapp("Hola, ¿qué fecha es tu evento?") == ["Hola, ¿qué fecha es tu evento?"]
    parts = split_for_whatsapp(LONG, limit=600)
    assert len(parts) == 2
    assert all(len(p) <= 600 for p in parts)
    assert parts[0].startswith("Te cuento") and parts[1].startswith("Para separar")
    assert " ".join(parts).split() == LONG.split()

    one_block = "Frase número uno del mensaje. " * 60
    parts = split_for_whatsapp(one_block, limit=300)
    assert len(parts) > 1 and all(len(p) <= 300 for p in parts)
    assert all(p.endswith(".") for p in parts), "se corta al final de una frase, no a mitad"
    assert " ".join(parts).split() == one_block.split()


def test_conflict_counts_as_session_opened_elsewhere():
    assert classify_disconnect({"statusReason": 440})[0] == "replaced"
    assert classify_disconnect({"lastDisconnect": {"error": {"message": "Stream Errored (conflict)"}}})[0] == "replaced"
    assert classify_disconnect({"statusReason": 408})[0] == "network"


def _load(db, conversation_id):
    from app.domain.entities import Conversation, Tenant, WhatsAppSession

    conversation = db.get(Conversation, conversation_id)
    tenant = db.get(Tenant, conversation.tenant_id)
    session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant.id).one()
    return tenant, session, conversation


@requires_db
def test_ai_reads_first_and_sends_long_replies_in_parts(chat):  # noqa: F811
    from app.application.whatsapp.whatsapp_service import send_text_message
    from app.infrastructure.persistence.database import SessionLocal

    (conversation_id, _), _ = chat
    with SessionLocal() as db:
        tenant, session, conversation = _load(db, conversation_id)
        kw = dict(tenant=tenant, session=session, conversation=conversation)
        with patch(SEND, side_effect=_sent) as send, patch(READ) as read:
            last = send_text_message(db, text=LONG, source="bot", **kw)
        texts = [c.args[2] for c in send.call_args_list]
        assert len(texts) == 2 and all(len(t) <= 600 for t in texts)
        assert all(c.kwargs["typing_ms"] > 0 for c in send.call_args_list)
        assert last.body == texts[-1]
        read.assert_called_once_with(session.instance_name, "573001234567@s.whatsapp.net", ["ORIG-CORTE"])

        with patch(SEND, side_effect=_sent) as send, patch(READ) as read:
            send_text_message(db, text="¿Para qué fecha sería?", source="bot", **kw)
        read.assert_not_called(), "lo ya respondido no se vuelve a marcar"

        with patch(SEND, side_effect=_sent) as send, patch(READ):
            send_text_message(db, text=LONG, source="bot", verbatim=True, **kw)
        assert send.call_count == 1, "un atajo del dueño sale tal cual"

        with patch(SEND, side_effect=_sent) as send, patch(READ) as read:
            send_text_message(db, text=LONG, source="agent", **kw)
        assert send.call_count == 1 and send.call_args.kwargs["typing_ms"] == 0
        read.assert_not_called()
        db.rollback()


@requires_db
def test_ai_waits_its_turn_past_the_per_minute_cap():
    from app.application.whatsapp.whatsapp_service import _bot_sends_key, wait_for_bot_send_slot
    from app.infrastructure.cache.redis_client import get_redis

    instance = f"t_{uuid.uuid4().hex[:12]}"
    naps: list[float] = []

    def nap(seconds: float) -> None:
        naps.append(seconds)
        get_redis().delete(_bot_sends_key(instance))

    with patch("app.application.whatsapp.whatsapp_service.settings.ai_max_sends_per_minute", 2):
        wait_for_bot_send_slot(instance, sleep=nap)
        wait_for_bot_send_slot(instance, sleep=nap)
        assert naps == []
        wait_for_bot_send_slot(instance, sleep=nap)
    assert len(naps) == 1 and 0.5 <= naps[0] <= 5.0


@requires_db
def test_short_drops_are_recorded_and_owner_hears_about_a_session_elsewhere(client, monkeypatch):
    from app.application.messaging.webhook_processor import process_evolution_webhook
    from app.domain.entities import AuditLog
    from app.domain.entities.enums import WhatsAppStatus
    from app.infrastructure.email import email_service
    from app.infrastructure.persistence.database import SessionLocal

    sent: list[dict] = []
    monkeypatch.setattr(email_service, "send_billing_email", lambda **kw: sent.append(kw))
    monkeypatch.setattr(
        "app.application.sync.sync_scheduler.ensure_whatsapp_sync_after_connect", lambda *a, **k: None
    )
    tenant_id, email = _register(client)
    with SessionLocal() as db:
        session = _linked_session(db, tenant_id, WhatsAppStatus.CONNECTED.value)
        instance, owner = session.instance_name, session.bound_owner_jid

    def update(data: dict) -> None:
        process_evolution_webhook(tenant_id, {"event": "connection.update", "instance": instance, "data": data})

    update({"state": "close", "statusReason": 440})
    update({"state": "open", "wuid": owner})
    update({"state": "close", "statusReason": 440})

    with SessionLocal() as db:
        actions = [
            a.action
            for a in db.query(AuditLog)
            .filter(AuditLog.tenant_id == tenant_id, AuditLog.action.in_(("system.whatsapp_blip", "system.whatsapp_replaced")))
            .order_by(AuditLog.created_at)
        ]
        blip = db.query(AuditLog).filter(AuditLog.tenant_id == tenant_id, AuditLog.action == "system.whatsapp_blip").one()
    assert actions.count("system.whatsapp_replaced") == 1, "un aviso por ventana, no uno por corte"
    assert blip.details["reason"] == "replaced" and blip.details["down_seconds"] < 60
    assert [m["to_email"] for m in sent] == [email]
    assert "otro equipo" in sent[0]["subject"]

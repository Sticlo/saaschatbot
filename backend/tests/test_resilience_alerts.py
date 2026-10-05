from __future__ import annotations

import logging
import time
import uuid
from unittest.mock import patch

import pytest

from app.config import settings
from app.infrastructure.ai import ai_text_provider, deepseek_client, gemini_client
from app.infrastructure.ai.deepseek_client import DeepSeekError
from app.application.monitoring import dev_alerts
from tests.conftest import requires_db


@pytest.fixture()
def captured_alerts(monkeypatch):
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        dev_alerts, "alert_dev", lambda key, title, detail="", **_kw: alerts.append((key, title)) or True
    )
    return alerts


@pytest.fixture()
def providers(monkeypatch):
    calls = {"deepseek": 0, "gemini": 0}
    monkeypatch.setattr(deepseek_client, "is_configured", lambda: True)
    monkeypatch.setattr(gemini_client, "is_configured", lambda: True)
    monkeypatch.setattr(ai_text_provider.time, "sleep", lambda _s: None)
    return calls


def _failing_deepseek(calls, status):
    def fake(*_a, **_kw):
        calls["deepseek"] += 1
        raise DeepSeekError("caído", status_code=status)

    return fake


def test_deepseek_transient_failure_retries_then_uses_gemini(monkeypatch, providers, captured_alerts):
    monkeypatch.setattr(deepseek_client, "chat_completion", _failing_deepseek(providers, 503))
    monkeypatch.setattr(gemini_client, "chat_text", lambda *_a, **_kw: "Hola, claro que sí")

    assert ai_text_provider.chat_completion([{"role": "user", "content": "hola"}]) == "Hola, claro que sí"
    assert providers["deepseek"] == 2


def test_deepseek_without_balance_skips_retry_and_alerts_dev(monkeypatch, providers, captured_alerts):
    monkeypatch.setattr(deepseek_client, "chat_completion", _failing_deepseek(providers, 402))
    monkeypatch.setattr(gemini_client, "chat_text", lambda *_a, **_kw: "respaldo")

    assert ai_text_provider.chat_completion([{"role": "user", "content": "hola"}]) == "respaldo"
    assert providers["deepseek"] == 1
    assert [key for key, _ in captured_alerts] == ["ai:deepseek:rejected"]


def test_all_providers_down_raises_and_alerts(monkeypatch, providers, captured_alerts):
    monkeypatch.setattr(deepseek_client, "chat_completion", _failing_deepseek(providers, 500))

    def gemini_down(*_a, **_kw):
        raise gemini_client.GeminiError("sin cuota", status_code=429)

    monkeypatch.setattr(gemini_client, "chat_text", gemini_down)

    with pytest.raises(DeepSeekError) as exc_info:
        ai_text_provider.chat_completion([{"role": "user", "content": "hola"}])
    assert isinstance(exc_info.value, ai_text_provider.AiUnavailableError)
    assert "ai:all_down" in [key for key, _ in captured_alerts]


def test_gemini_chat_text_maps_roles_and_system(monkeypatch):
    seen = {}
    monkeypatch.setattr(gemini_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        gemini_client, "_post_generate", lambda *, model, payload, timeout: seen.update(payload=payload) or "ok"
    )

    gemini_client.chat_text(
        [
            {"role": "system", "content": "Eres el asistente"},
            {"role": "assistant", "content": "¡Hola! Bienvenido"},
            {"role": "user", "content": "precio"},
            {"role": "user", "content": "del menú"},
        ]
    )

    payload = seen["payload"]
    assert payload["systemInstruction"]["parts"][0]["text"] == "Eres el asistente"
    roles = [c["role"] for c in payload["contents"]]
    assert roles == ["user", "model", "user"]
    assert payload["contents"][-1]["parts"][0]["text"] == "precio\ndel menú"


def test_alerts_are_grouped_and_report_repeats(monkeypatch):
    sent: list[dev_alerts.DevAlert] = []
    monkeypatch.setattr(settings, "ops_alerts_enabled", True)
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(dev_alerts, "_dispatch", sent.append)
    key = f"test:{uuid.uuid4().hex}"

    assert dev_alerts.alert_dev(key, "Algo falló", throttle_seconds=60) is True
    assert dev_alerts.alert_dev(key, "Algo falló", throttle_seconds=60) is False
    assert dev_alerts.alert_dev(key, "Algo falló", throttle_seconds=60) is False
    assert len(sent) == 1

    from app.infrastructure.cache.redis_client import get_redis

    get_redis().delete(f"ops:alert:{key}")
    assert dev_alerts.alert_dev(key, "Algo falló", throttle_seconds=60) is True
    assert sent[-1].repeated == 2
    assert "Se repitió 2" in dev_alerts.format_alert(sent[-1])
    get_redis().delete(f"ops:alert:{key}", f"ops:alert:n:{key}")


def test_alerts_never_fire_outside_production(monkeypatch):
    sent: list = []
    monkeypatch.setattr(settings, "ops_alerts_enabled", True)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(dev_alerts, "_dispatch", sent.append)

    assert dev_alerts.alert_dev(f"test:{uuid.uuid4().hex}", "local") is False
    assert sent == []


def test_logged_exceptions_reach_the_dev(captured_alerts):
    handler = dev_alerts.DevAlertLogHandler()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.getLogger("app.test").makeRecord(
            "app.test", logging.ERROR, __file__, 1, "Error procesando %s", ("x",), exc_info=__import__("sys").exc_info()
        )
    handler.handle(record)

    assert len(captured_alerts) == 1
    assert captured_alerts[0][0].startswith("log:")


def test_delayed_ai_retries_are_promoted_when_due(monkeypatch):
    from app.application.ai import ai_queue_service as queue
    from app.infrastructure.cache.redis_client import get_redis

    # Colas propias: un worker local corriendo no debe robarse el job.
    tag = uuid.uuid4().hex
    monkeypatch.setattr(queue, "AI_REPLY_QUEUE", f"test:ai:{tag}")
    monkeypatch.setattr(queue, "AI_DELAYED_QUEUE", f"test:ai:delayed:{tag}")
    ids = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    queue.schedule_ai_retry(*ids, delay_seconds=100)
    client = get_redis()
    try:
        assert queue.promote_due_ai_retries(now=time.time()) == 0
        assert queue.promote_due_ai_retries(now=time.time() + 200) == 1
        items = [i.decode() if isinstance(i, bytes) else i for i in client.lrange(queue.AI_REPLY_QUEUE, 0, -1)]
        assert len(items) == 1 and str(ids[2]) in items[0]
        assert client.zcard(queue.AI_DELAYED_QUEUE) == 0
    finally:
        client.delete(queue.AI_REPLY_QUEUE, queue.AI_DELAYED_QUEUE)


@requires_db
def test_ai_status_never_exposes_provider_errors(client, monkeypatch):
    from tests.test_security_hardening import _register

    def must_not_ping(*_a, **_kw):
        raise AssertionError("el panel no debe gastar una petición a DeepSeek por carga")

    monkeypatch.setattr(deepseek_client, "chat_completion", must_not_ping)
    token = _register(client)["access_token"]
    res = client.get("/api/v1/ai/status", headers={"Authorization": f"Bearer {token}"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["provider_ok"] is True
    assert body["provider_error"] is None


def test_unhandled_errors_show_calm_message_and_alert_dev(captured_alerts):
    import asyncio
    import json

    from starlette.requests import Request

    from app.presentation import main

    request = Request({"type": "http", "method": "POST", "path": "/api/v1/x", "headers": [], "query_string": b""})
    monkey_alerts: list[str] = []
    original = main.alert_dev
    main.alert_dev = lambda key, *_a, **_kw: monkey_alerts.append(key)
    try:
        response = asyncio.run(main.unhandled_error(request, ValueError("db explotó")))
    finally:
        main.alert_dev = original

    assert response.status_code == 500
    detail = json.loads(response.body)["detail"]
    assert "explotó" not in detail and "inconveniente momentáneo" in detail
    assert monkey_alerts == ["http500:POST:/api/v1/x:ValueError"]


def _seed_qualify_chat(db):
    from app.domain.entities import Conversation, Message, Tenant, TenantProfile, WhatsAppSession
    from app.domain.entities.enums import AiMode, ConversationMode, MessageDirection, MessageSource, WhatsAppStatus

    tenant = Tenant(business_name="Resiliencia", slug=f"r-{uuid.uuid4().hex[:8]}", ai_global_enabled=True)
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
        body="¿Cuánto cuesta la noche?",
        status="received",
    )
    db.add(msg)
    db.commit()
    return tenant, conv, msg


def _fake_whatsapp_send(sent: list[str]):
    from app.domain.entities import Message
    from app.domain.entities.enums import MessageDirection

    def fake(db, *, tenant, session, conversation, text, source):
        row = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            direction=MessageDirection.OUT.value,
            source=source,
            body=text,
            status="sent",
        )
        db.add(row)
        db.flush()
        sent.append(text)
        return row

    return fake


@requires_db
@patch("app.application.ai.ai_service.publish_panel_event")
@patch("app.application.ai.ai_service.publish_conversation_updated")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.classify_inbound_message")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_outage_is_invisible_then_hands_off_to_owner(
    _sleep, mock_classify, mock_generate, _publish_conv, mock_panel_event, monkeypatch, captured_alerts
):
    from app.application.ai import ai_queue_service, ai_service
    from app.application.ai.ai_service import process_ai_reply
    from app.domain.entities.enums import ConversationMode
    from app.infrastructure.persistence.database import SessionLocal

    mock_classify.return_value = {"category": "duda", "reason": "precio"}
    mock_generate.side_effect = ai_text_provider.AiUnavailableError("todo caído")
    sent: list[str] = []
    retries: list[float] = []
    monkeypatch.setattr(ai_service, "send_text_message", _fake_whatsapp_send(sent))
    monkeypatch.setattr(
        ai_queue_service, "schedule_ai_retry", lambda *_a, delay_seconds: retries.append(delay_seconds)
    )

    with SessionLocal() as db:
        tenant, conv, msg = _seed_qualify_chat(db)

        def run():
            process_ai_reply(db, tenant_id=tenant.id, conversation_id=conv.id, message_id=msg.id)
            db.commit()

        run()
        assert sent == [], "el primer fallo se reintenta en silencio"
        assert retries == [20]

        run()
        assert len(sent) == 1 and sent[0] in (
            ai_service.HOLDING_REPLIES_FIRST_CONTACT + ai_service.HOLDING_REPLIES
        )
        assert "Cuéntame qué te gustaría saber" not in sent[0]

        run()
        assert len(sent) == 1, "el «dame un momentico» no se repite y no bloquea el reintento"
        assert retries == [20, 60, 180]

        run()
        db.refresh(conv)
        assert sent[-1] == ai_service.OUTAGE_HANDOFF_REPLY
        assert conv.mode == ConversationMode.MANUAL.value

    events = [call.args[1]["type"] for call in mock_panel_event.call_args_list]
    assert "ai.handoff" in events
    assert any(key.startswith("ai:handoff:") for key, _ in captured_alerts)


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.classify_inbound_message")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_recovers_after_holding_message(_sleep, mock_classify, mock_generate, mock_send_reply, monkeypatch):
    from app.application.ai import ai_queue_service, ai_service
    from app.application.ai.ai_service import process_ai_reply
    from app.application.ai.ai_shortcut_service import AiGeneratedReply
    from app.infrastructure.persistence.database import SessionLocal

    mock_classify.return_value = {"category": "duda", "reason": "precio"}
    sent: list[str] = []
    monkeypatch.setattr(ai_service, "send_text_message", _fake_whatsapp_send(sent))
    monkeypatch.setattr(ai_queue_service, "schedule_ai_retry", lambda *_a, delay_seconds: None)

    with SessionLocal() as db:
        tenant, conv, msg = _seed_qualify_chat(db)

        def run():
            process_ai_reply(db, tenant_id=tenant.id, conversation_id=conv.id, message_id=msg.id)
            db.commit()

        mock_generate.side_effect = ai_text_provider.AiUnavailableError("caído")
        run()
        run()
        assert len(sent) == 1

        mock_generate.side_effect = None
        mock_generate.return_value = AiGeneratedReply(message="La noche cuesta $180.000 😊")
        run()

    mock_send_reply.assert_called_once()

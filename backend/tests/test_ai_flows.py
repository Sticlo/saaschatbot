"""Flujos: el saludo sale solo en el primer mensaje y «pregunta por…» lo elige la IA; los pasos van exactos y en orden."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.application.ai import ai_conversation_service as conv
from app.application.ai import ai_flow_service as fs
from app.application.ai import ai_shortcut_service as ss
from app.application.ai.ai_shortcut_service import AiGeneratedReply, parse_ai_reply
from app.application.outbound import quick_shortcut_service
from app.application.whatsapp import whatsapp_service

SHORTCUTS = [
    {"id": "menu", "label": "Menú", "type": "image", "image_path": "x/menu.jpg"},
    {"id": "precios", "label": "Precios", "type": "document", "file_path": "x/precios.pdf"},
]

GREETING = {
    "id": "hola",
    "name": "Bienvenida",
    "trigger": "first_contact",
    "steps": [
        {"type": "text", "text": "¡Hola! Bienvenido a Café Luna ☕"},
        {"type": "shortcut", "shortcut_id": "menu"},
        {"type": "shortcut", "shortcut_id": "precios"},
    ],
}
PRICES = {
    "id": "precios-flow",
    "name": "Precios",
    "trigger": "asks_about",
    "topic": "precios, cuánto cuesta",
    "steps": [{"type": "text", "text": "Te comparto los precios 👇"}, {"type": "shortcut", "shortcut_id": "precios"}],
}


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, ex=None, nx=False):
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def exists(self, key):
        return int(key in self.store)


@pytest.fixture
def fake_redis(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(fs, "get_redis", lambda: redis)
    return redis


def test_normalize_keeps_order_and_drops_deleted_shortcuts():
    flows = fs.normalize_flows(
        [{**GREETING, "steps": [*GREETING["steps"], {"type": "shortcut", "shortcut_id": "borrado"}]}],
        {"menu", "precios"},
    )
    assert [s.get("shortcut_id") or s["text"] for s in flows[0]["steps"]] == [
        "¡Hola! Bienvenido a Café Luna ☕",
        "menu",
        "precios",
    ]
    assert flows[0]["active"] is True


def test_normalize_rejects_what_would_not_work():
    ids = {"menu", "precios"}
    with pytest.raises(fs.FlowError, match="Solo puede haber un saludo"):
        fs.normalize_flows([GREETING, {**GREETING, "id": "otro"}], ids)
    with pytest.raises(fs.FlowError, match="por qué tiene que preguntar"):
        fs.normalize_flows([{**PRICES, "topic": " "}], ids)
    with pytest.raises(fs.FlowError, match="no tiene nada para enviar"):
        fs.normalize_flows([{**PRICES, "steps": [{"type": "text", "text": "  "}]}], ids)
    # Un saludo apagado no choca con el activo.
    assert len(fs.normalize_flows([GREETING, {**GREETING, "id": "viejo", "active": False}], ids)) == 2


def test_greeting_goes_only_on_first_contact():
    flows = fs.normalize_flows([GREETING, PRICES], {"menu", "precios"})
    first = fs.plan_flows(flows, SHORTCUTS, is_first_contact=True, sent=set())
    assert first.auto_id == "hola"
    assert "No saludes otra vez" in first.prompt and "atajo «Menú» (foto)" in first.prompt
    assert first.valid_ids == {"precios-flow"}

    later = fs.plan_flows(flows, SHORTCUTS, is_first_contact=False, sent=set())
    assert later.auto_id is None and "primer mensaje" not in later.prompt
    assert 'flow_id="precios-flow"' in later.prompt and "precios, cuánto cuesta" in later.prompt

    done = fs.plan_flows(flows, SHORTCUTS, is_first_contact=False, sent={"precios-flow"})
    assert done.prompt == "" and not done.valid_ids


def test_parse_accepts_flow_with_empty_message():
    raw = json.dumps({"message": "", "flow_id": "precios-flow", "shortcut_id": None, "stage": "interesado"})
    reply = parse_ai_reply(raw, valid_ids=frozenset(), valid_flow_ids=frozenset({"precios-flow"}))
    assert reply.flow_id == "precios-flow" and reply.message == "" and reply.stage == "interesado"

    invented = parse_ai_reply(
        json.dumps({"message": "Hola", "flow_id": "inventado"}),
        valid_ids=frozenset(),
        valid_flow_ids=frozenset({"precios-flow"}),
    )
    assert invented.flow_id is None and invented.message == "Hola"


def test_ai_reply_carries_greeting_even_if_it_stays_quiet(monkeypatch):
    seen = {}

    def fake_completion(messages, **kwargs):
        seen["system"] = messages[0]["content"]
        return json.dumps({"message": "", "shortcut_id": None, "stage": "conversando"})

    monkeypatch.setattr(conv, "chat_completion", fake_completion)
    plan = fs.FlowPlan(prompt="\n\nBLOQUE-FLUJOS", valid_ids=frozenset(), auto_id="hola")
    reply = conv._complete_reply(system="Eres la IA", history=[], shortcuts=[], flows=plan)
    assert reply.flow_id == "hola" and reply.message == ""
    assert "BLOQUE-FLUJOS" in seen["system"]


def test_flow_steps_go_in_order_before_the_ai_message_and_only_once(monkeypatch, fake_redis):
    sent = []
    monkeypatch.setattr(fs, "get_flows", lambda db, tenant_id: fs.normalize_flows([GREETING], {"menu", "precios"}))
    monkeypatch.setattr(quick_shortcut_service, "get_shortcuts", lambda db, tenant_id: SHORTCUTS)

    def fake_text(db, *, text, verbatim=False, **kwargs):
        sent.append(("texto", text, verbatim))

    def fake_shortcut(db, *, shortcut, **kwargs):
        sent.append(("atajo", shortcut["id"]))

    monkeypatch.setattr(whatsapp_service, "send_text_message", fake_text)
    monkeypatch.setattr(ss, "send_text_message", fake_text)
    monkeypatch.setattr(ss, "send_shortcut_content", fake_shortcut)

    conversation = SimpleNamespace(id=uuid.uuid4())
    tenant = SimpleNamespace(id=uuid.uuid4())
    reply = AiGeneratedReply(message="¿Para cuántas personas?", flow_id="hola")
    ss.send_reply_with_shortcut(None, tenant=tenant, session=None, conversation=conversation, reply=reply)
    assert sent == [
        ("texto", "¡Hola! Bienvenido a Café Luna ☕", True),
        ("atajo", "menu"),
        ("atajo", "precios"),
        ("texto", "¿Para cuántas personas?", False),
    ]

    sent.clear()
    ss.send_reply_with_shortcut(
        None, tenant=tenant, session=None, conversation=conversation, reply=AiGeneratedReply(message="", flow_id="hola")
    )
    assert sent == []

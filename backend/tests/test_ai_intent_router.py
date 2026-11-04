"""«¿Cómo se separa?», «¿cómo contratamos?»: el mismo flujo, sin listas de sinónimos."""
from __future__ import annotations

from types import SimpleNamespace

from app.application.ai import ai_intent_router as router
from app.application.ai.ai_flow_service import plan_flows

TOPICS = (
    ("res_anim", "como se separa o reserva el paquete de solo animación"),
    ("res_deco", "como se separa o reserva el paquete de decoracion y animacion"),
    ("baby", "paquetes de baby shower"),
)


def _msg(direction: str, body: str):
    return SimpleNamespace(direction=direction, body=body, transcript=None)


def test_router_returns_only_known_flows():
    valid = frozenset(t[0] for t in TOPICS)
    assert router.parse_route('{"flow_id": "res_deco"}', valid) == "res_deco"
    assert router.parse_route('```json\n{"flow_id": "baby"}\n```', valid) == "baby"
    assert router.parse_route('{"flow_id": "inventado"}', valid) is None
    assert router.parse_route('{"flow_id": null}', valid) is None
    assert router.parse_route("no sé", valid) is None


def test_router_sees_the_conversation_and_every_topic(monkeypatch):
    seen = {}

    def fake(messages, **kw):
        seen["messages"] = messages
        seen["kw"] = kw
        return '{"flow_id": "res_deco"}'

    monkeypatch.setattr(router, "chat_completion", fake)
    history = [
        _msg("in", "me interesa el de decoración y animación"),
        _msg("out", "¡Genial! Ese incluye decoración temática y 2 recreadores."),
        _msg("in", "listo, ¿cómo contratamos?"),
    ]
    assert router.route_flow(TOPICS, history) == "res_deco"
    assert all(topic in seen["messages"][0]["content"] for _, topic in TOPICS)
    assert "Cliente: listo, ¿cómo contratamos?" in seen["messages"][1]["content"]
    assert seen["kw"]["temperature"] == 0.0


def test_router_stays_quiet_when_it_cannot_help(monkeypatch):
    def broken(*_a, **_kw):
        raise RuntimeError("sin red")

    monkeypatch.setattr(router, "chat_completion", broken)
    assert router.route_flow(TOPICS, [_msg("in", "¿cómo se separa?")]) is None
    assert router.route_flow((), [_msg("in", "hola")]) is None
    assert router.route_flow(TOPICS, [_msg("out", "Hola")]) is None


def test_routed_flow_is_sent_even_if_the_main_ai_forgets(monkeypatch):
    from app.application.ai import ai_conversation_service as conv

    monkeypatch.setattr(conv, "route_flow", lambda topics, history: "res_anim")
    monkeypatch.setattr(conv, "chat_completion", lambda *_a, **_kw: '{"message":"¡Claro! Te cuento."}')
    plan = plan_flows(
        [{"id": i, "trigger": "asks_about", "topic": t, "steps": [{"type": "text", "text": "Para separar: abono del 50%"}]} for i, t in TOPICS],
        [],
        is_first_contact=False,
        sent=set(),
    )
    reply = conv._complete_reply(system="Base", history=[_msg("in", "¿cómo apartamos?")], shortcuts=[], flows=plan)
    assert reply.flow_id == "res_anim"


def test_a_flow_already_sent_is_still_known_word_for_word():
    flows = [
        {"id": "res_anim", "trigger": "asks_about", "topic": "como se separa el paquete de animación",
         "steps": [{"type": "text", "text": "Para separar se abona el 50% por Nequi y se envía el formulario."}]},
    ]
    plan = plan_flows(flows, [], is_first_contact=False, sent={"res_anim"})
    assert plan.valid_ids == frozenset() and plan.topics == ()
    assert "Para separar se abona el 50% por Nequi y se envía el formulario." in plan.prompt
    assert "no los reenvíes" in plan.prompt

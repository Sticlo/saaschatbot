from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.application.ai.ai_human_service import (
    Turn,
    chat_cap_reason,
    is_endless_goodbye,
    other_side_bot_reason,
    robot_phrases,
    says_it_is_a_bot,
    style_correction,
    without_robot_phrases,
)

T0 = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)


def _chat(*turns):
    """turns: (segundos, 'in'/'out', texto)."""
    return [Turn(inbound=d == "in", body=b, at=T0 + timedelta(seconds=s)) for s, d, b in turns]


def test_robot_phrases_are_caught_and_human_ones_pass():
    assert robot_phrases("Entiendo perfectamente tu situación. Quedo atento!!")
    assert robot_phrases("No dudes en escribirme, ¿en qué más te puedo ayudar?")
    assert not robot_phrases("Listo, el sábado a las 3 te queda perfecto. ¿Te lo separo?")


def test_style_correction_asks_for_a_rewrite_only_when_needed():
    assert "robot" in style_correction("¡Excelente pregunta! El paquete vale $300.000")
    assert style_correction("El paquete vale $300.000. ¿Para qué fecha sería?") == ""
    previous = "Te cuento que sí tenemos. ¿Para qué fecha sería tu evento?"
    assert "misma pregunta" in style_correction("Vale $300.000. ¿Para qué fecha sería el evento?", previous)


def test_last_resort_cleanup_keeps_the_useful_part():
    text = "Entiendo perfectamente. El paquete vale $300.000. ¿Te lo separo?"
    assert without_robot_phrases(text) == "El paquete vale $300.000. ¿Te lo separo?"
    assert without_robot_phrases("Quedo atento") == "Quedo atento"


def test_endless_goodbye_is_not_answered_but_a_new_question_is():
    bye = "Con gusto, Laura. ¡Que tengas un lindo día! 🌸"
    assert is_endless_goodbye(bye, "Gracias igualmente 🙏")
    assert is_endless_goodbye(bye, "Muchas gracias, muy amable, bendiciones")
    assert not is_endless_goodbye(bye, "Gracias, ¿y el sábado abren?")
    assert not is_endless_goodbye(bye, "gracias, entonces el viernes paso por el pedido")
    assert not is_endless_goodbye("¿Te lo separo para el sábado?", "gracias")


def test_bot_that_introduces_itself_but_not_people_who_ask():
    assert says_it_is_a_bot("Hola, soy Sol, la asistente virtual de Clínica Norte.")
    assert says_it_is_a_bot("Esta es una respuesta automática. Te contactaremos pronto.")
    assert not says_it_is_a_bot("¿Eres un bot?")
    assert not says_it_is_a_bot("Soy la asistente de gerencia, ¿con quién hablo?")
    assert not says_it_is_a_bot("Ya tenemos un asistente virtual, gracias")


def test_repeated_auto_message_after_our_replies_is_a_bot():
    auto = "Gracias por comunicarte con Clínica Norte, en breve te atenderemos"
    loop = _chat((0, "in", auto), (30, "out", "Hola, ¿con quién hablo?"), (40, "in", auto),
                 (70, "out", "¿Me regalas tu nombre?"), (80, "in", auto))
    assert other_side_bot_reason(loop)


def test_client_insisting_because_nobody_answered_is_not_a_bot():
    text = "Hola, me pueden dar información por favor"
    assert other_side_bot_reason(_chat((0, "in", text), (60, "in", text), (120, "in", text))) is None


def test_long_texts_answered_in_seconds_twice_is_a_bot():
    long = "Gracias por su mensaje. Nuestro horario de atención es de lunes a viernes de 8 a 5, un asesor le responderá."
    fast = _chat((0, "out", "Hola"), (5, "in", long), (40, "out", "¿Con quién hablo?"), (44, "in", long + " Saludos."))
    assert other_side_bot_reason(fast)
    other = "Hablas con Marcela, la encargada de compras. Cuéntame qué ofrecen y los precios, que lo reviso con calma."
    slow = _chat((0, "out", "Hola"), (60, "in", long), (120, "out", "¿Con quién hablo?"), (200, "in", other))
    assert other_side_bot_reason(slow) is None


def test_robotic_reply_is_rewritten_once(monkeypatch):
    import json

    from app.application.ai import ai_conversation_service as svc

    answers = iter([
        json.dumps({"message": "¡Excelente pregunta! El paquete vale $300.000. Quedo atento."}),
        json.dumps({"message": "El paquete vale $300.000. ¿Para qué fecha lo necesitas?"}),
    ])
    calls = []

    def fake_completion(messages, **_):
        calls.append(messages)
        return next(answers)

    monkeypatch.setattr(svc, "chat_completion", fake_completion)
    reply = svc._complete_reply(system="Eres vendedor.", history=[], shortcuts=[])
    assert reply.message == "El paquete vale $300.000. ¿Para qué fecha lo necesitas?"
    assert len(calls) == 2 and "robot" in calls[1][-1]["content"]
    assert "persona real del equipo" in calls[0][0]["content"]


def test_chat_caps_count_replies_not_message_parts():
    parts = [T0 + timedelta(seconds=s) for s in (0, 2, 4, 6, 8)]
    assert chat_cap_reason(parts, now=T0 + timedelta(minutes=1)) is None
    replies = [T0 + timedelta(minutes=m) for m in range(8)]
    assert chat_cap_reason(replies, now=T0 + timedelta(minutes=9))

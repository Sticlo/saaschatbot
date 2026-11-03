from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

from app.application.ai.ai_memory_service import notes_prompt_block, parse_notes, remember
from app.application.ai.ai_shortcut_service import parse_ai_reply, reply_format_instruction
from app.application.whatsapp import whatsapp_service as wa


def _conv(notes: str | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(), client_notes=notes, contact_jid="", contact_phone="+573001112233"
    )


def test_notes_are_cleaned_deduplicated_and_never_keep_numbers():
    notes = parse_notes(["  Su hija Sofía cumple 5 ", "su hija sofía cumple 5", "Cédula 1.023.456.789", 7, "Le encanta Frozen"])
    assert notes == ("Su hija Sofía cumple 5", "Le encanta Frozen")
    assert parse_notes(None) is None and parse_notes("Frozen") is None and parse_notes([]) is None


def test_reply_carries_the_full_list_of_notes():
    raw = json.dumps({"message": "¡Qué lindo! Para Sofi la opción 3 queda perfecta", "notes": ["Fiesta sorpresa", "Hija Sofía"]})
    reply = parse_ai_reply(raw, valid_ids=frozenset())
    assert reply.notes == ("Fiesta sorpresa", "Hija Sofía")
    assert parse_ai_reply(json.dumps({"message": "Listo", "notes": None}), valid_ids=frozenset()).notes is None


def test_the_model_is_asked_for_notes():
    assert '"notes":null' in reply_format_instruction(has_shortcuts=False, booking_enabled=False)


def test_what_it_knows_goes_to_the_prompt_with_the_rules_to_use_it_quietly():
    block = notes_prompt_block(_conv("Hija Sofía, cumple 5\nLe encanta Frozen"))
    assert "- Hija Sofía, cumple 5" in block and "- Le encanta Frozen" in block
    assert "como me dijiste" in block and "máximo un detalle por mensaje" in block
    assert notes_prompt_block(_conv()) == ""


def test_notes_are_replaced_only_when_the_ai_learned_something():
    conv = _conv("Hija Sofía")
    assert not remember(conv, None)
    assert conv.client_notes == "Hija Sofía"
    assert remember(conv, ("Hija Sofía", "Es sorpresa"))
    assert conv.client_notes == "Hija Sofía\nEs sorpresa"
    assert not remember(conv, ("Hija Sofía", "Es sorpresa"))


def test_time_already_typing_while_thinking_is_not_waited_again(monkeypatch):
    conv_id = uuid.uuid4()
    assert wa.remaining_typing_ms(conv_id, 3000) == 3000
    wa._typing_since[conv_id] = time.monotonic() - 2
    assert 900 <= wa.remaining_typing_ms(conv_id, 3000) <= 1000
    assert conv_id not in wa._typing_since
    wa._typing_since[conv_id] = time.monotonic() - 5
    assert wa.remaining_typing_ms(conv_id, 3000) == 0
    wa._typing_since[conv_id] = time.monotonic() - 600
    assert wa.remaining_typing_ms(conv_id, 3000) == 3000


def test_typing_shows_while_the_ai_thinks(monkeypatch):
    calls, read = [], []
    monkeypatch.setattr(wa, "uses_waha", lambda: False)
    monkeypatch.setattr(wa, "gateway_send_typing", lambda instance, jid, delay_ms: calls.append((instance, jid, delay_ms)))
    monkeypatch.setattr(wa, "mark_customer_messages_read", lambda db, session, conversation: read.append(conversation.id))
    monkeypatch.setattr(wa.settings, "ai_thinking_typing_ms", 20000)
    conv = _conv()
    wa.start_typing(None, session=SimpleNamespace(instance_name="t_1"), conversation=conv)
    for _ in range(50):
        if calls:
            break
        time.sleep(0.01)
    assert calls == [("t_1", "573001112233@s.whatsapp.net", 20000)]
    assert read == [conv.id] and conv.id in wa._typing_since
    wa._typing_since.pop(conv.id, None)

    monkeypatch.setattr(wa.settings, "ai_thinking_typing_ms", 0)
    wa.start_typing(None, session=SimpleNamespace(instance_name="t_1"), conversation=_conv())
    assert len(calls) == 1

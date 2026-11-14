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


def test_colombian_holidays_follow_the_monday_law():
    from datetime import date

    from app.shared.core.holidays_co import holiday_name, holidays

    assert holiday_name(date(2026, 11, 2)) == "Todos los Santos"
    assert holiday_name(date(2026, 4, 2)) == "Jueves Santo"
    assert holiday_name(date(2026, 4, 3)) == "Viernes Santo"
    assert holiday_name(date(2026, 5, 18)) == "Ascensión del Señor"
    assert holiday_name(date(2026, 6, 8)) == "Corpus Christi"
    assert holiday_name(date(2026, 1, 12)) == "Reyes Magos"
    assert holiday_name(date(2026, 11, 3)) is None
    assert len(holidays(2026)) == 18


def test_ai_knows_holidays_and_late_night():
    from datetime import datetime, timezone

    from app.application.ai.ai_conversation_service import date_context

    # 2 nov 2026, 11:30 p. m. en Bogotá
    night = date_context(datetime(2026, 11, 3, 4, 30, tzinfo=timezone.utc))
    assert "Hoy es festivo en Colombia (Todos los Santos)" in night
    assert "Es de noche" in night
    afternoon = date_context(datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc))
    assert "festivo" not in afternoon and "Es de noche" not in afternoon
    eve = date_context(datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc))
    assert "Mañana es festivo en Colombia (Todos los Santos)" in eve


def test_a_customer_who_returns_after_weeks_is_greeted_as_returning():
    from datetime import datetime, timedelta, timezone

    from app.application.ai.ai_memory_service import returning_block

    now = datetime.now(timezone.utc)

    def msg(direction, ago):
        return SimpleNamespace(direction=direction, created_at=now - ago)

    old = [msg("in", timedelta(days=100)), msg("out", timedelta(days=99)), msg("in", timedelta(minutes=2)), msg("in", timedelta(minutes=1))]
    assert "hace 3 meses" in returning_block(old)
    recent = [msg("in", timedelta(days=3)), msg("out", timedelta(days=3)), msg("in", timedelta(minutes=1))]
    assert returning_block(recent) == ""
    assert returning_block([msg("in", timedelta(minutes=1))]) == ""


def test_ai_stays_on_the_business_and_does_not_assume_facts():
    from app.application.ai.ai_conversation_service import SAFETY_RULES

    assert "problemas de matemáticas" in SAFETY_RULES
    assert "con eso no le puedes ayudar" in SAFETY_RULES
    assert "no lo supongas" in SAFETY_RULES


def test_memory_reset_survives_as_a_marker(monkeypatch):
    from datetime import datetime, timezone

    from app.application.ai import ai_service

    store: dict[str, str] = {}
    fake = SimpleNamespace(set=lambda k, v: store.__setitem__(k, v), get=lambda k: store.get(k))
    monkeypatch.setattr(ai_service, "get_redis", lambda: fake)
    conv_id = uuid.uuid4()
    assert ai_service._memory_from(conv_id) is None
    at = datetime(2026, 11, 14, 4, 30, tzinfo=timezone.utc)
    ai_service.reset_ai_memory(conv_id, at)
    assert ai_service._memory_from(conv_id) == at


def test_owner_alert_carries_what_the_ai_knows():
    from app.application.ai.ai_service import _notes_line

    assert _notes_line(_conv("Hija Sofía, cumple 5\nLe encanta Frozen")) == "Lo que sabemos: Hija Sofía, cumple 5 · Le encanta Frozen\n"
    assert _notes_line(_conv(None)) == ""

from __future__ import annotations

import json
from datetime import date

from app.application.ai.ai_appointment_service import (
    append_appointment_instructions,
    match_slot_from_message,
    parse_book_slot,
    valid_book_slot_keys,
)
from app.application.ai.ai_shortcut_service import parse_ai_reply


def test_valid_book_slot_keys():
    slots = [
        {"date": "2026-06-30", "start": "10:00", "end": "10:30"},
        {"date": "2026-06-30", "start": "11:00", "end": "11:30"},
    ]
    keys = valid_book_slot_keys(slots)
    assert ("2026-06-30", "10:00", "10:30") in keys
    assert len(keys) == 2


def test_parse_book_slot_accepts_valid_key():
    keys = frozenset({("2026-06-30", "10:00", "10:30")})
    slot = parse_book_slot(
        {"date": "2026-06-30", "start": "10:00", "end": "10:30", "notes": "corte"},
        valid_keys=keys,
    )
    assert slot is not None
    assert slot.notes == "corte"


def test_parse_book_slot_rejects_unknown_slot():
    keys = frozenset({("2026-06-30", "10:00", "10:30")})
    slot = parse_book_slot(
        {"date": "2026-06-30", "start": "12:00", "end": "12:30"},
        valid_keys=keys,
    )
    assert slot is None


def test_parse_ai_reply_with_book_slot():
    raw = json.dumps(
        {
            "message": "Te separo mañana a las 10.",
            "shortcut_id": None,
            "book_slot": {
                "date": "2026-06-30",
                "start": "10:00",
                "end": "10:30",
                "notes": "corte",
            },
        },
        ensure_ascii=False,
    )
    keys = frozenset({("2026-06-30", "10:00", "10:30")})
    result = parse_ai_reply(raw, valid_ids=frozenset(), valid_book_keys=keys)
    assert result.message == "Te separo mañana a las 10."
    assert result.book_slot is not None
    assert result.book_slot.start == "10:00"


def test_match_slot_from_message_by_time():
    today = date(2026, 6, 29)
    slots = [
        {
            "date": "2026-06-30",
            "start": "15:00",
            "end": "15:30",
            "label": "Mañana 15:00–15:30",
        }
    ]
    matched = match_slot_from_message("mañana a las 3 pm", slots, today=today)
    assert matched is not None
    assert matched["start"] == "15:00"


def test_detect_scheduling_interest_ignores_cuesta():
    from app.application.ai.ai_appointment_service import detect_scheduling_interest

    assert detect_scheduling_interest("¿Cuánto cuesta?") is False
    assert detect_scheduling_interest("Quiero una cita mañana") is True


def test_append_appointment_instructions_lists_slots():
    slots = [
        {
            "date": "2026-06-30",
            "start": "10:00",
            "end": "10:30",
            "label": "Mañana 10:00–10:30",
        }
    ]
    out = append_appointment_instructions("Base prompt.", slots)
    assert "10:00" in out
    assert "book_slot" in out

from __future__ import annotations

import json

from app.application.ai.ai_shortcut_service import (
    AiGeneratedReply,
    append_shortcuts_instructions,
    format_shortcut_line,
    parse_ai_reply,
)


def test_format_shortcut_line_text():
    line = format_shortcut_line(
        {"id": "abc", "label": "Menú", "type": "text", "text": "Almuerzo $18.000"}
    )
    assert 'id="abc"' in line
    assert 'botón="Menú"' in line
    assert "Almuerzo" in line


def test_append_shortcuts_adds_json_instruction():
    base = "Eres un asistente."
    shortcuts = [{"id": "x1", "label": "Fotos", "type": "image", "image_path": "a/b.jpg"}]
    out = append_shortcuts_instructions(base, shortcuts)
    assert "Atajos rápidos" in out
    assert 'id="x1"' in out
    assert "shortcut_id" in out


def test_parse_ai_reply_with_shortcut():
    raw = json.dumps(
        {"message": "Te comparto el menú 👇", "shortcut_id": "menu-1"},
        ensure_ascii=False,
    )
    result = parse_ai_reply(raw, valid_ids=frozenset({"menu-1"}))
    assert result == AiGeneratedReply(
        message="Te comparto el menú 👇",
        shortcut_id="menu-1",
    )


def test_parse_ai_reply_rejects_unknown_shortcut():
    raw = json.dumps({"message": "Hola", "shortcut_id": "fake"})
    result = parse_ai_reply(raw, valid_ids=frozenset({"real"}))
    assert result.shortcut_id is None
    assert result.message == "Hola"


def test_parse_ai_reply_plain_text_when_no_shortcuts():
    result = parse_ai_reply("Hola, ¿en qué te ayudo?", valid_ids=frozenset())
    assert result.message == "Hola, ¿en qué te ayudo?"
    assert result.shortcut_id is None


def test_parse_ai_reply_fallback_on_invalid_json():
    result = parse_ai_reply("Respuesta normal sin json", valid_ids=frozenset({"a"}))
    assert result.message == "Respuesta normal sin json"

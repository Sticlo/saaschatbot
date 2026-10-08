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


MENU = {"id": "m1", "label": "menu", "type": "image", "image_path": "assets/x/menu.png"}
HOURS = {"id": "h1", "label": "Horarios", "type": "text", "text": "Lun a Sáb 8 a 6"}


def test_promising_the_menu_without_attaching_it_attaches_it():
    from app.application.ai.ai_shortcut_service import infer_promised_shortcut

    shortcuts = [MENU, HOURS]
    assert infer_promised_shortcut("Claro, Sebastián. Te lo envío ahora mismo.", "Si me gustaría ver el menu", shortcuts) == "m1"
    assert infer_promised_shortcut("Te comparto el menú con los servicios y precios.", "Que servicios tienen?", shortcuts) == "m1"
    assert infer_promised_shortcut("Aquí tienes nuestros horarios", "a qué hora abren", shortcuts) == "h1"


def test_no_attachment_when_nothing_was_promised_or_it_is_unclear():
    from app.application.ai.ai_shortcut_service import infer_promised_shortcut

    assert infer_promised_shortcut("El corte vale $800", "cuánto vale el corte según el menú", [MENU]) is None
    assert infer_promised_shortcut("Te lo envío ahora", "dame la dirección", [HOURS]) is None
    both = [MENU, {"id": "m2", "label": "menu niños", "type": "image", "image_path": "x"}]
    assert infer_promised_shortcut("Te envío el menú", "el menu", both) is None


def test_single_catalog_is_attached_when_the_client_asks_for_prices():
    from app.application.ai.ai_shortcut_service import infer_promised_shortcut

    carta = {"id": "c1", "label": "Carta", "type": "document", "file_path": "x.pdf"}
    assert infer_promised_shortcut("¡Claro! Te la paso 👇", "me pasas los precios?", [carta, HOURS]) == "c1"


def test_question_marks_and_did_not_arrive_mean_resend():
    from app.application.ai.ai_shortcut_service import asks_to_resend
    from app.application.messaging.message_service import is_reaction_only

    assert asks_to_resend("???")
    assert asks_to_resend("no me llegó nada")
    assert asks_to_resend("mándalo de nuevo porfa")
    assert not asks_to_resend("¿Cuánto vale el corte?")
    assert is_reaction_only("???") is False
    assert is_reaction_only("👍") is True

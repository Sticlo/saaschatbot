from __future__ import annotations

from app.application.outbound.bait_template_service import _normalize_buttons
from app.application.outbound.outbound_service import render_bait_message


def test_render_bait_message_variables():
    text = render_bait_message(
        "Hola{name_part}! Somos {business}, hablamos con {name}.",
        contact_name="Ana García",
        business_name="Acme",
    )
    assert " Ana" in text
    assert "Acme" in text
    assert "Ana García" in text


def test_normalize_buttons_caps_at_three():
    raw = [{"label": f"B{i}", "value": f"v{i}"} for i in range(5)]
    out = _normalize_buttons(raw)
    assert out is not None
    assert len(out) == 3
    assert out[0]["label"] == "B0"

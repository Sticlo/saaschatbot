from __future__ import annotations

import uuid

from app.application.outbound.quick_shortcut_service import _normalize


def test_normalize_text_shortcut():
    rows = _normalize([
        {"label": "Saludo", "type": "text", "text": "Hola!"},
        {"label": "", "type": "text", "text": "x"},
    ])
    assert len(rows) == 1
    assert rows[0]["label"] == "Saludo"
    assert rows[0]["type"] == "text"


def test_normalize_image_shortcut():
    path = f"assets/{uuid.uuid4()}/menu.jpg"
    rows = _normalize([
        {"label": "Menú", "type": "image", "image_path": path},
        {"label": "Sin foto", "type": "image"},
    ])
    assert len(rows) == 1
    assert rows[0]["label"] == "Menú"
    assert rows[0]["image_path"] == path

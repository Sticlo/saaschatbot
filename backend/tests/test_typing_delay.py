"""La IA muestra «escribiendo…» antes de responder; lo que escribe el dueño sale de una."""
from __future__ import annotations

from unittest.mock import patch

from app.application.whatsapp.whatsapp_service import typing_delay_ms
from app.infrastructure.evolution.evolution_client import evolution_client


def test_typing_grows_with_the_message_and_has_a_cap():
    short, long = typing_delay_ms("Hola"), typing_delay_ms("x" * 400)
    assert 1200 < short < 2000
    assert long == 4500
    with patch("app.application.whatsapp.whatsapp_service.settings.ai_typing_max_ms", 0):
        assert typing_delay_ms("Hola") == 0


def test_delay_goes_to_evolution_only_when_asked():
    with patch.object(evolution_client, "_request", return_value={}) as request:
        evolution_client.send_text("t_1", "573001112233", "Hola", typing_ms=1500)
        evolution_client.send_text("t_1", "573001112233", "Hola")
    with_typing, without = (c.kwargs["json"] for c in request.call_args_list)
    assert with_typing["delay"] == 1500
    assert "delay" not in without
    assert request.call_args_list[0].kwargs["timeout"] == 31.5

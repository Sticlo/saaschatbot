"""Cientos de respuestas oficiales: al prompt solo van las que tienen que ver con lo que preguntan."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.application.ai.ai_answer_rules_service import (
    AnswerRulesError,
    answer_rules_block,
    normalize_rules,
    relevant_rules,
)

RAW = {
    "rules": [
        {"name": "Alquiler independiente de sonido", "triggers": "alquilar solo sonido, alquilan cabina, renta de parlantes",
         "response": "No alquilamos los equipos por separado.", "mode": "direct", "materialId": "ADULTOS-SYL-2026", "enabled": True},
        {"name": "Humo de revelación", "triggers": "incluye humo, viene con humo, extintor de humo",
         "response": "Los paquetes no incluyen humo de base.", "mode": "review", "materialId": "REVELACION-2026"},
        {"name": "Cancelación", "triggers": ["cancelar evento", "devolucion del abono", "hasta cuando cancelo"],
         "response": "La cancelación debe pedirse antes de los últimos 5 días.", "mode": "direct"},
        {"name": "Apagada", "triggers": "x", "response": "no", "enabled": "False"},
        {"name": "", "triggers": "", "response": "sin pregunta"},
    ]
}


def _msg(direction, body):
    return SimpleNamespace(direction=direction, body=body)


def test_rules_are_cleaned_from_any_reasonable_file():
    rules = normalize_rules(RAW)
    assert [r["name"] for r in rules] == ["Alquiler independiente de sonido", "Humo de revelación", "Cancelación"]
    assert rules[1]["review"] is True and rules[0]["review"] is False
    assert rules[2]["triggers"] == "cancelar evento, devolucion del abono, hasta cuando cancelo"
    with pytest.raises(AnswerRulesError):
        normalize_rules({"rules": [{"name": "sin respuesta"}]})


def test_finds_the_rule_even_when_asked_differently():
    rules = normalize_rules(RAW)
    assert relevant_rules(rules, "¿ustedes alquilan solo el sonido?")[0]["name"] == "Alquiler independiente de sonido"
    assert relevant_rules(rules, "el humo viene incluido?")[0]["name"] == "Humo de revelación"
    assert relevant_rules(rules, "hasta cuándo puedo cancelar")[0]["name"] == "Cancelación"
    assert relevant_rules(rules, "hola buenas tardes") == []


def test_only_relevant_rules_reach_the_prompt():
    rules = normalize_rules(RAW)
    history = [_msg("out", "Te comparto el portafolio de revelación"), _msg("in", "y el humo viene?")]
    block = answer_rules_block(rules, history)
    assert "Los paquetes no incluyen humo de base." in block and "CONFIRMAR" in block
    assert "No alquilamos" not in block and "últimos 5 días" not in block
    assert answer_rules_block([], history) == "" and answer_rules_block(rules, []) == ""


def test_owner_uploads_and_deletes_rules(client):
    from tests.test_phase1 import _register

    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    res = client.put("/api/v1/ai-answer-rules", json={"rules": RAW["rules"]}, headers=headers)
    assert res.status_code == 200 and res.json()["count"] == 3 and res.json()["review"] == 1
    assert client.get("/api/v1/ai-answer-rules", headers=headers).json()["count"] == 3
    bad = client.put("/api/v1/ai-answer-rules", json={"rules": "nada"}, headers=headers)
    assert bad.status_code == 400
    assert client.delete("/api/v1/ai-answer-rules", headers=headers).json()["count"] == 0

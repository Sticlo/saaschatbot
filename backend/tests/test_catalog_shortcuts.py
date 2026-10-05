"""Catálogo/carta en PDF o foto: la IA lo conoce y lo envía una sola vez por chat."""
from __future__ import annotations

import shutil
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.application.ai import ai_shortcut_service
from app.application.ai.ai_shortcut_service import (
    AiGeneratedReply,
    annotate_sent_shortcuts,
    append_shortcuts_instructions,
    format_shortcut_line,
    mark_shortcut_sent,
    shortcut_sent_at,
)
from app.application.outbound.quick_shortcut_service import _normalize
from tests.conftest import requires_db

_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"

CATALOG_SHORTCUT = {
    "id": "cat-1",
    "label": "Catálogo",
    "type": "document",
    "file_path": "assets/x/cat.pdf",
    "file_name": "catalogo-katshoes.pdf",
    "content": "Tenis blancos $120.000 tallas 36-42\nBotas negras $180.000",
}


def test_normalize_keeps_document_shortcut_with_content():
    rows = _normalize([
        dict(CATALOG_SHORTCUT),
        {"id": "sin-archivo", "label": "Roto", "type": "document"},
    ])
    assert len(rows) == 1
    assert rows[0]["type"] == "document"
    assert rows[0]["file_name"] == "catalogo-katshoes.pdf"
    assert "Tenis blancos" in rows[0]["content"]


def test_prompt_includes_catalog_knowledge_and_sent_marker():
    line = format_shortcut_line({**CATALOG_SHORTCUT, "already_sent": True})
    assert "archivo PDF (catalogo-katshoes.pdf)" in line
    assert "ya enviado en este chat" in line

    prompt = append_shortcuts_instructions("Eres un asistente.", [CATALOG_SHORTCUT])
    assert "Tenis blancos $120.000" in prompt
    assert "No inventes productos" in prompt
    assert "NO lo vuelvas a enviar" in prompt


def test_catalog_knowledge_is_capped():
    big = [
        {**CATALOG_SHORTCUT, "id": f"c{i}", "content": "x" * 4000}
        for i in range(4)
    ]
    prompt = append_shortcuts_instructions("Base", big)
    assert prompt.count("x") <= ai_shortcut_service._CATALOG_KNOWLEDGE_CHARS + 50


def test_text_shortcuts_add_no_catalog_block():
    prompt = append_shortcuts_instructions(
        "Base", [{"id": "t", "label": "Horario", "type": "text", "text": "8am-6pm"}]
    )
    assert "Lo que sabes de los productos" not in prompt


def test_annotate_marks_only_sent_shortcuts():
    conv_id = uuid.uuid4()
    mark_shortcut_sent(conv_id, "cat-1")
    rows = annotate_sent_shortcuts(
        [CATALOG_SHORTCUT, {"id": "otro", "label": "Menú", "type": "text", "text": "x"}],
        conv_id,
    )
    assert rows[0].get("already_sent") is True
    assert "already_sent" not in rows[1]
    assert "already_sent" not in CATALOG_SHORTCUT


def _fake_reply_env(monkeypatch):
    sent = {"text": [], "shortcut": []}
    monkeypatch.setattr(
        ai_shortcut_service, "send_text_message", lambda db, **kw: sent["text"].append(kw["text"])
    )
    monkeypatch.setattr(ai_shortcut_service, "find_shortcut", lambda db, tid, sid: dict(CATALOG_SHORTCUT))
    monkeypatch.setattr(
        ai_shortcut_service, "send_bot_shortcut", lambda db, **kw: sent["shortcut"].append(kw["shortcut"]["id"])
    )
    return sent


def test_recently_sent_catalog_is_not_resent(monkeypatch):
    sent = _fake_reply_env(monkeypatch)
    conv = SimpleNamespace(id=uuid.uuid4())
    tenant = SimpleNamespace(id=uuid.uuid4())
    reply = AiGeneratedReply(message="Te lo comparto 👇", shortcut_id="cat-1")

    ai_shortcut_service.send_reply_with_shortcut(
        None, tenant=tenant, session=None, conversation=conv, reply=reply
    )
    assert sent["shortcut"] == ["cat-1"]

    mark_shortcut_sent(conv.id, "cat-1")
    ai_shortcut_service.send_reply_with_shortcut(
        None, tenant=tenant, session=None, conversation=conv, reply=reply
    )
    assert sent["shortcut"] == ["cat-1"]
    assert len(sent["text"]) == 2


def test_catalog_can_be_resent_after_guard_window(monkeypatch):
    sent = _fake_reply_env(monkeypatch)
    conv = SimpleNamespace(id=uuid.uuid4())
    old = time.time() - ai_shortcut_service.SHORTCUT_RESEND_GUARD_SECONDS - 5
    monkeypatch.setattr(ai_shortcut_service, "shortcut_sent_at", lambda cid, sid: old)

    ai_shortcut_service.send_reply_with_shortcut(
        None,
        tenant=SimpleNamespace(id=uuid.uuid4()),
        session=None,
        conversation=conv,
        reply=AiGeneratedReply(message="De nuevo 👇", shortcut_id="cat-1"),
    )
    assert sent["shortcut"] == ["cat-1"]


def test_send_shortcut_content_sends_document_and_marks_it(monkeypatch):
    conv_id = uuid.uuid4()
    calls = []

    def fake_send_document(db, **kw):
        calls.append(kw)
        return SimpleNamespace(conversation_id=conv_id)

    monkeypatch.setattr(ai_shortcut_service, "send_document_message", fake_send_document)
    ai_shortcut_service.send_shortcut_content(
        None,
        tenant=None,
        session=None,
        conversation=SimpleNamespace(id=conv_id),
        shortcut=dict(CATALOG_SHORTCUT),
        source="bot",
    )
    assert calls[0]["file_path"] == "assets/x/cat.pdf"
    assert calls[0]["file_name"] == "catalogo-katshoes.pdf"
    assert shortcut_sent_at(conv_id, "cat-1") is not None


def test_evolution_sends_pdf_as_document(monkeypatch):
    from app.application.whatsapp import whatsapp_gateway
    from app.infrastructure.evolution.evolution_client import evolution_client

    captured = {}
    monkeypatch.setattr(whatsapp_gateway, "uses_waha", lambda: False)
    monkeypatch.setattr(
        evolution_client,
        "_request",
        lambda method, path, **kw: captured.update(path=path, json=kw["json"]) or {"key": {"id": "X"}},
    )
    whatsapp_gateway.send_document(
        "inst", "573001112233", data_b64="QUJD", mimetype="application/pdf", filename="catalogo.pdf"
    )
    assert captured["path"] == "/message/sendMedia/inst"
    assert captured["json"]["mediatype"] == "document"
    assert captured["json"]["fileName"] == "catalogo.pdf"
    assert captured["json"]["mimetype"] == "application/pdf"


def _register(client: TestClient) -> dict:
    tag = uuid.uuid4().hex[:8]
    res = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Catálogo {tag}",
            "owner_name": "Dueña",
            "email": f"catalogo-{tag}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    assert res.status_code == 201, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture()
def assets_cleanup():
    from app.application.outbound import tenant_asset_service

    paths: list[str] = []
    yield paths
    for path in paths:
        tenant_dir = tenant_asset_service._ASSETS_ROOT / path.split("/")[1]
        shutil.rmtree(tenant_dir, ignore_errors=True)


@requires_db
def test_upload_pdf_reads_catalog_and_saves_shortcut(client: TestClient, monkeypatch, assets_cleanup):
    from app.infrastructure.ai import gemini_client

    seen = {}
    monkeypatch.setattr(gemini_client, "is_configured", lambda: True)

    def fake_extract(b64, mime, timeout=90.0):
        seen["mime"] = mime
        return "Tenis blancos $120.000"

    monkeypatch.setattr(gemini_client, "extract_catalog", fake_extract)
    headers = _register(client)

    res = client.post(
        "/api/v1/quick-shortcuts/files",
        headers=headers,
        files={"file": ("Catálogo KatShoes.pdf", _PDF, "application/pdf")},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assets_cleanup.append(data["path"])
    assert data["type"] == "document"
    assert data["file_name"].endswith(".pdf")
    assert data["content"] == "Tenis blancos $120.000"
    assert data["content_ok"] is True
    assert seen["mime"] == "application/pdf"

    saved = client.put(
        "/api/v1/quick-shortcuts",
        headers=headers,
        json={"shortcuts": [{
            "label": "Catálogo",
            "type": "document",
            "file_path": data["path"],
            "file_name": data["file_name"],
            "content": data["content"],
        }]},
    )
    assert saved.status_code == 200, saved.text
    row = saved.json()[0]
    assert row["type"] == "document"
    assert row["content"] == "Tenis blancos $120.000"
    assert row["file_url"].endswith(".pdf")


@requires_db
def test_upload_rejects_fake_pdf_and_foreign_paths(client: TestClient, monkeypatch):
    from app.infrastructure.ai import gemini_client

    monkeypatch.setattr(gemini_client, "is_configured", lambda: False)
    headers = _register(client)

    fake = client.post(
        "/api/v1/quick-shortcuts/files",
        headers=headers,
        files={"file": ("catalogo.pdf", b"MZ\x90\x00 no soy pdf", "application/pdf")},
    )
    assert fake.status_code == 400

    foreign = client.put(
        "/api/v1/quick-shortcuts",
        headers=headers,
        json={"shortcuts": [{
            "label": "Robado",
            "type": "document",
            "file_path": f"assets/{uuid.uuid4()}/otro.pdf",
            "file_name": "otro.pdf",
        }]},
    )
    assert foreign.status_code == 400


@requires_db
def test_upload_without_gemini_still_saves_file(client: TestClient, monkeypatch, assets_cleanup):
    from app.infrastructure.ai import gemini_client

    monkeypatch.setattr(gemini_client, "is_configured", lambda: False)
    headers = _register(client)
    res = client.post(
        "/api/v1/quick-shortcuts/files",
        headers=headers,
        files={"file": ("carta.pdf", _PDF, "application/pdf")},
    )
    assert res.status_code == 200, res.text
    assets_cleanup.append(res.json()["path"])
    assert res.json()["content_ok"] is False

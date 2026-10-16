"""Notas de voz y archivos que el dueño manda desde el chat del panel, como en WhatsApp."""
from __future__ import annotations

import uuid
from unittest.mock import patch

from tests.conftest import requires_db
from tests.test_chat_image_send import _register, chat  # noqa: F401

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 128
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 128
PDF = b"%PDF-1.7\n" + b"0" * 128


def _voice(client, conversation_id, headers, data: bytes):
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages/audio",
        files={"file": ("nota.webm", data, "audio/webm")},
        headers=headers,
    )


def _file(client, conversation_id, headers, *, data: bytes, name: str, caption: str = ""):
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages/file",
        files={"file": (name, data, "application/octet-stream")},
        data={"caption": caption},
        headers=headers,
    )


@requires_db
@patch("app.application.messaging.media_cache.save_media_from_webhook")
@patch("app.application.whatsapp.whatsapp_service.gateway_send_voice", return_value={"key": {"id": "WA-VOZ-1"}})
def test_voice_note_reaches_the_client_and_plays_in_the_chat(mock_send, mock_cache, client, chat):
    from app.domain.entities import Message
    from app.infrastructure.persistence.database import SessionLocal

    conversation_id, headers = chat
    response = _voice(client, conversation_id, headers, WEBM)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["body"] == "[audio]"
    assert body["source"] == "agent"
    assert mock_send.call_args.kwargs["mimetype"] == "audio/webm"
    assert mock_cache.call_args.args[1] == "WA-VOZ-1"
    with SessionLocal() as db:
        assert db.get(Message, uuid.UUID(body["id"])).evolution_message_id == "WA-VOZ-1"


@requires_db
@patch("app.application.messaging.media_cache.save_media_from_webhook")
@patch("app.application.whatsapp.whatsapp_service.gateway_send_voice", return_value={"key": {"id": "WA-VOZ-2"}})
def test_iphone_recordings_are_accepted_and_garbage_is_not(mock_send, _cache, client, chat):
    conversation_id, headers = chat

    assert _voice(client, conversation_id, headers, M4A).status_code == 201
    assert mock_send.call_args.kwargs["mimetype"] == "audio/mp4"

    mock_send.reset_mock()
    rejected = _voice(client, conversation_id, headers, b"no soy audio" * 20)
    assert rejected.status_code == 400
    mock_send.assert_not_called()


@requires_db
@patch("app.application.whatsapp.whatsapp_service.gateway_send_document", return_value={"key": {"id": "WA-DOC-1"}})
def test_pdf_from_the_plus_button_reaches_the_client(mock_send, client, chat):
    conversation_id, headers = chat
    response = _file(client, conversation_id, headers, data=PDF, name="Cotización boda.pdf", caption="Te la paso")

    assert response.status_code == 201, response.text
    assert response.json()["body"] == "📄 Cotización boda.pdf\nTe la paso"
    kwargs = mock_send.call_args.kwargs
    assert kwargs["mimetype"] == "application/pdf"
    assert kwargs["filename"] == "Cotización boda.pdf"


@requires_db
@patch("app.application.whatsapp.whatsapp_service.gateway_send_document")
def test_unsupported_or_fake_files_never_reach_whatsapp(mock_send, client, chat):
    conversation_id, headers = chat

    exe = _file(client, conversation_id, headers, data=b"MZ" + b"\x00" * 64, name="programa.exe")
    assert exe.status_code == 400
    fake_pdf = _file(client, conversation_id, headers, data=b"hola" * 20, name="factura.pdf")
    assert fake_pdf.status_code == 400
    mock_send.assert_not_called()


@requires_db
@patch("app.application.whatsapp.whatsapp_service.gateway_send_voice")
@patch("app.application.whatsapp.whatsapp_service.gateway_send_document")
def test_another_business_cannot_send_voice_or_files_to_this_chat(mock_doc, mock_voice, client, chat):
    conversation_id, _headers = chat
    intruder = {"Authorization": f"Bearer {_register(client)['access_token']}"}

    assert _voice(client, conversation_id, intruder, WEBM).status_code == 404
    assert _file(client, conversation_id, intruder, data=PDF, name="x.pdf").status_code == 404
    mock_voice.assert_not_called()
    mock_doc.assert_not_called()

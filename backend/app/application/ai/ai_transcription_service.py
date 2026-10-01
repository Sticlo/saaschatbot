from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.application.messaging.media_fetch_service import MediaFetchError, fetch_message_media
from app.application.messaging.message_service import _detect_media_type_from_body
from app.application.realtime.realtime_service import publish_message_event
from app.config import settings
from app.domain.entities import Conversation, Message, Tenant
from app.infrastructure.ai import gemini_client

log = logging.getLogger(__name__)


def is_voice_note(message: Message) -> bool:
    return _detect_media_type_from_body(message.body) in ("audio", "ptt")


def is_image(message: Message) -> bool:
    # Los stickers también son imágenes, pero no esperan respuesta.
    return _detect_media_type_from_body(message.body) == "image"


def can_transcribe(message: Message) -> bool:
    return (
        (is_voice_note(message) or is_image(message))
        and message.transcript is None
        and gemini_client.is_configured()
    )


def ensure_transcript(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
) -> Optional[str]:
    """Transcribe la nota de voz o describe la imagen una sola vez y lo guarda en message.transcript.
    None = no se pudo (se reintenta)."""
    if message.transcript is not None:
        return message.transcript
    if not can_transcribe(message):
        return None

    voice = is_voice_note(message)
    kind = "nota de voz" if voice else "imagen"
    try:
        media = fetch_message_media(db, tenant_id=tenant.id, conversation=conversation, message=message)
    except MediaFetchError as exc:
        log.warning("No se pudo descargar %s msg=%s: %s", kind, message.id, exc)
        return None

    max_mb = settings.ai_audio_max_mb if voice else settings.ai_image_max_mb
    if len(media["base64"]) > int(max_mb * 1024 * 1024 * 4 / 3):
        log.info("%s demasiado grande para procesar msg=%s", kind, message.id)
        message.transcript = ""
        db.flush()
        return ""

    try:
        if voice:
            text = gemini_client.transcribe_audio(media["base64"], media["mimetype"])
        else:
            text = gemini_client.describe_image(media["base64"], media["mimetype"])
    except gemini_client.GeminiError as exc:
        log.warning("Gemini no procesó %s msg=%s: %s", kind, message.id, exc)
        return None

    message.transcript = text[:4000]
    db.flush()
    publish_message_event(tenant, conversation, message, event_type="message.updated")
    log.info("%s procesada conv=%s msg=%s chars=%s", kind, conversation.id, message.id, len(text))
    return message.transcript

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import Conversation, Message, WhatsAppSession

MEDIA_TYPES = ("image", "video", "audio", "sticker", "document", "ptt")

MEDIA_MIME = {
    "image": "image/jpeg",
    "sticker": "image/webp",
    "video": "video/mp4",
    "audio": "audio/ogg",
    "ptt": "audio/ogg",
    "document": "application/octet-stream",
}


class MediaFetchError(Exception):
    def __init__(self, message: str, *, status_code: int = 404):
        super().__init__(message)
        self.status_code = status_code


def media_type_of(message: Message) -> str:
    body = (message.body or "").strip().lower()
    return next((mt for mt in MEDIA_TYPES if body.startswith(f"[{mt}")), "document")


def fetch_message_media(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    conversation: Conversation,
    message: Message,
) -> dict:
    """{base64, media_type, mimetype}: primero caché local del webhook, luego Evolution."""
    from app.application.messaging.media_cache import get_media_from_cache
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client
    from app.infrastructure.evolution.evolution_store import fetch_evolution_message_by_id
    from app.shared.core.phone import phone_to_evolution_number

    if not message.evolution_message_id:
        raise MediaFetchError("Este mensaje no tiene media asociada")

    media_type = media_type_of(message)
    cached = get_media_from_cache(str(tenant_id), message.evolution_message_id)
    if cached:
        return {"base64": cached["base64"], "media_type": media_type, "mimetype": cached["mimetype"]}

    session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()
    if session is None:
        raise MediaFetchError("Sesión WhatsApp no encontrada")

    contact_jid = conversation.contact_jid or ""
    if not contact_jid and conversation.contact_phone:
        contact_jid = f"{phone_to_evolution_number(conversation.contact_phone)}@s.whatsapp.net"

    evo_msg = fetch_evolution_message_by_id(
        settings.evolution_database_url,
        session.instance_name,
        message.evolution_message_id,
        contact_jid,
    )

    if (not evo_msg or not evo_msg.get("message")) and contact_jid:
        found = evolution_client.find_message_by_key(
            session.instance_name,
            message_id=message.evolution_message_id,
            remote_jid=contact_jid,
        )
        if found and isinstance(found, dict):
            key = found.get("key") if isinstance(found.get("key"), dict) else {}
            body = found.get("message") if isinstance(found.get("message"), dict) else {}
            evo_msg = {"key": key, "message": body}

    if not evo_msg or not evo_msg.get("message"):
        raise MediaFetchError("Media no encontrada en Evolution")

    try:
        result = evolution_client.get_media_base64(session.instance_name, evo_msg)
    except EvolutionAPIError as exc:
        raise MediaFetchError(f"Evolution no pudo descargar el media: {exc}", status_code=502) from exc

    b64 = result.get("base64") or result.get("data") or ""
    if not b64:
        raise MediaFetchError("Evolution no retornó datos de media", status_code=502)
    mime = result.get("mimetype") or MEDIA_MIME.get(media_type, "application/octet-stream")
    return {"base64": b64, "media_type": media_type, "mimetype": mime}

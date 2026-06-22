from __future__ import annotations

import uuid

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.deps import RequireAgent, RequireViewer
from app.core.phone import is_owner_display_name, is_owner_jid, normalize_phone
from app.config import settings
from app.database import get_db
from app.models import Conversation, Message, Tenant, WhatsAppSession
from app.models.enums import MessageSource
from app.services.whatsapp_status import can_send_whatsapp
from app.schemas.whatsapp import (
    ConversationAiUpdate,
    ConversationModeUpdate,
    ConversationResponse,
    MessageResponse,
    SendMessageRequest,
    serialize_conversation,
)
from app.services.realtime_service import publish_conversation_updated
from app.services.evolution_client import EvolutionAPIError, evolution_client
from app.services.tenant_service import log_audit
from app.services.whatsapp_service import refresh_session_status, send_text_message

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _to_conversation_response(conversation: Conversation) -> ConversationResponse:
    return ConversationResponse.model_validate(serialize_conversation(conversation))


@router.get("", response_model=list[ConversationResponse])
def list_conversations(
    current: RequireViewer,
    db: Session = Depends(get_db),
    archived: Optional[bool] = None,
):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == current.tenant_id)
        .first()
    )
    from app.services.whatsapp_conversation_service import conversations_visible_for_tenant

    if tenant is None or not conversations_visible_for_tenant(
        db, tenant=tenant, session=session
    ):
        return []

    query = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == current.tenant_id,
            Conversation.whatsapp_connection_id == session.active_connection_id,
        )
    )
    if archived is True:
        query = query.filter(Conversation.is_archived.is_(True))
    elif archived is False:
        query = query.filter(Conversation.is_archived.is_(False))

    rows = query.order_by(
        Conversation.last_message_at.desc().nullslast(), Conversation.created_at.desc()
    ).all()
    owner_jid = session.bound_owner_jid or ""
    owner_phone = normalize_phone(session.phone_number or "")
    owner_names: set[str] = set()
    if owner_jid and settings.evolution_database_url:
        from app.services.evolution_store import fetch_contact_push_name

        push = fetch_contact_push_name(
            settings.evolution_database_url, session.instance_name, owner_jid
        )
        if push:
            owner_names.add(push)
    visible = []
    for row in rows:
        if owner_phone and normalize_phone(row.contact_phone) == owner_phone:
            continue
        if is_owner_jid(row.contact_jid or "", owner_jid=owner_jid, owner_phone=owner_phone):
            continue
        if owner_names and is_owner_display_name(row.contact_name, owner_names):
            continue
        visible.append(row)
    return [_to_conversation_response(row) for row in visible]


@router.get("/{conversation_id}/messages", response_model=list[MessageResponse])
def list_messages(
    conversation_id: uuid.UUID,
    current: RequireViewer,
    db: Session = Depends(get_db),
):
    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            Conversation.tenant_id == current.tenant_id,
        )
        .first()
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")

    messages = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.asc())
        .all()
    )
    deduped: list[Message] = []
    seen_ids: set[str] = set()
    seen_bodies: set[str] = set()
    for msg in messages:
        if msg.evolution_message_id:
            if msg.evolution_message_id in seen_ids:
                continue
            seen_ids.add(msg.evolution_message_id)
        else:
            body_key = f"{msg.body}|{msg.created_at.isoformat()}|{msg.direction}"
            if body_key in seen_bodies:
                continue
            seen_bodies.add(body_key)
        deduped.append(msg)
    messages = deduped
    if conversation.unread_count:
        conversation.unread_count = 0
        db.commit()
    return messages


@router.post("/{conversation_id}/messages", response_model=MessageResponse, status_code=201)
def send_message(
    conversation_id: uuid.UUID,
    body: SendMessageRequest,
    request: Request,
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == current.tenant_id)
        .first()
    )
    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            Conversation.tenant_id == current.tenant_id,
        )
        .first()
    )
    if tenant is None or session is None or conversation is None:
        raise HTTPException(status_code=404, detail="Conversación o WhatsApp no encontrado")

    try:
        refresh_session_status(db, tenant, session)
        db.commit()
        db.refresh(tenant)
    except EvolutionAPIError:
        db.rollback()

    ok, reason = can_send_whatsapp(tenant.whatsapp_status)
    if not ok:
        raise HTTPException(status_code=409, detail=reason)

    try:
        message = send_text_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            text=body.text.strip(),
            source=MessageSource.AGENT.value,
        )
        log_audit(
            db,
            tenant_id=tenant.id,
            user_id=current.id,
            action="message.sent_manual",
            details={"conversation_id": str(conversation.id)},
            ip_address=request.client.host if request.client else None,
        )
        db.commit()
        db.refresh(message)
    except EvolutionAPIError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return message


@router.patch("/{conversation_id}/mode", response_model=ConversationResponse)
def update_conversation_mode(
    conversation_id: uuid.UUID,
    body: ConversationModeUpdate,
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            Conversation.tenant_id == current.tenant_id,
        )
        .first()
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")

    conversation.mode = body.mode
    db.commit()
    db.refresh(conversation)
    publish_conversation_updated(current.tenant_id, conversation)
    return _to_conversation_response(conversation)


@router.patch("/{conversation_id}/ai", response_model=ConversationResponse)
def update_conversation_ai(
    conversation_id: uuid.UUID,
    body: ConversationAiUpdate,
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            Conversation.tenant_id == current.tenant_id,
        )
        .first()
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")

    conversation.ai_active = body.ai_active
    db.commit()
    db.refresh(conversation)
    publish_conversation_updated(current.tenant_id, conversation)
    return _to_conversation_response(conversation)


# ──────────────────────────────────────────────────────────────
# Media
# ──────────────────────────────────────────────────────────────

_MEDIA_TYPES = {"image", "video", "audio", "sticker", "document", "ptt"}

MEDIA_MIME = {
    "image": "image/jpeg",
    "sticker": "image/webp",
    "video": "video/mp4",
    "audio": "audio/ogg",
    "ptt": "audio/ogg",
    "document": "application/octet-stream",
}


@router.get("/{conversation_id}/messages/{message_id}/media")
def get_message_media(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    current: RequireViewer,
    db: Session = Depends(get_db),
):
    """Descarga media de un mensaje desde Evolution API y lo retorna en base64."""
    from fastapi.responses import JSONResponse

    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            Conversation.tenant_id == current.tenant_id,
        )
        .first()
    )
    message = (
        db.query(Message)
        .filter(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
        )
        .first()
    )
    if conversation is None or message is None:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")

    if not message.evolution_message_id:
        raise HTTPException(status_code=404, detail="Este mensaje no tiene media asociada")

    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == current.tenant_id)
        .first()
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Sesión WhatsApp no encontrada")

    from app.core.phone import phone_to_evolution_number
    from app.services.evolution_store import fetch_evolution_message_by_id
    from app.services.media_cache import get_media_from_cache

    contact_jid = conversation.contact_jid or ""
    if not contact_jid and conversation.contact_phone:
        contact_jid = f"{phone_to_evolution_number(conversation.contact_phone)}@s.whatsapp.net"

    # Detectar tipo desde el body del mensaje almacenado
    body_lower = (message.body or "").strip().lower()
    media_type = "document"
    for mt in _MEDIA_TYPES:
        if body_lower.startswith(f"[{mt}"):
            media_type = mt
            break

    # 1. Buscar primero en caché local de disco (guardado en el webhook)
    if message.evolution_message_id:
        cached = get_media_from_cache(str(current.tenant_id), message.evolution_message_id)
        if cached:
            return JSONResponse({
                "base64": cached["base64"],
                "media_type": media_type,
                "mimetype": cached["mimetype"],
            })

    evo_msg = fetch_evolution_message_by_id(
        settings.evolution_database_url,
        session.instance_name,
        message.evolution_message_id,
        contact_jid,
    )

    # Fallback: buscar via Evolution API si no está en DB
    if (not evo_msg or not evo_msg.get("message")) and contact_jid:
        found = evolution_client.find_message_by_key(
            session.instance_name,
            message_id=message.evolution_message_id,
            remote_jid=contact_jid,
        )
        if found and isinstance(found, dict):
            key = found.get("key") or {}
            msg_body = found.get("message") or {}
            if not isinstance(key, dict):
                key = {}
            if not isinstance(msg_body, dict):
                msg_body = {}
            evo_msg = {"key": key, "message": msg_body}

    if not evo_msg or not evo_msg.get("message"):
        raise HTTPException(status_code=404, detail="Media no encontrada en Evolution")

    try:
        result = evolution_client.get_media_base64(session.instance_name, evo_msg)
    except EvolutionAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Evolution no pudo descargar el media: {exc}") from exc

    b64 = result.get("base64") or result.get("data") or ""
    mime = result.get("mimetype") or MEDIA_MIME.get(media_type, "application/octet-stream")
    if not b64:
        raise HTTPException(status_code=502, detail="Evolution no retornó datos de media")

    return JSONResponse({"base64": b64, "media_type": media_type, "mimetype": mime})

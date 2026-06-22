from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.deps import RequireAgent, RequireViewer
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
from app.services.evolution_client import EvolutionAPIError
from app.services.tenant_service import log_audit
from app.services.whatsapp_service import refresh_session_status, send_text_message

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _to_conversation_response(conversation: Conversation) -> ConversationResponse:
    return ConversationResponse.model_validate(serialize_conversation(conversation))


@router.get("", response_model=list[ConversationResponse])
def list_conversations(
    current: RequireViewer,
    db: Session = Depends(get_db),
    archived: bool | None = None,
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
    return [_to_conversation_response(row) for row in rows]


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

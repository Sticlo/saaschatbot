from __future__ import annotations

import uuid

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.shared.core.deps import RequireAgent, RequireViewer
from app.shared.core.phone import is_owner_display_name, is_owner_jid, is_placeholder_contact_name, is_valid_whatsapp_phone, normalize_phone
from app.config import settings
from app.infrastructure.persistence.database import get_db
from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
from app.domain.entities.enums import MessageSource
from app.application.whatsapp.whatsapp_status import can_send_whatsapp
from app.presentation.schemas.whatsapp import (
    ConversationAiUpdate,
    ConversationLiveSyncResponse,
    ConversationModeUpdate,
    ConversationResponse,
    MessageResponse,
    SendMessageRequest,
    serialize_conversation,
)
from app.application.realtime.realtime_service import publish_conversation_updated
from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client
from app.application.billing.tenant_service import log_audit
from app.application.whatsapp.whatsapp_service import refresh_session_status, send_text_message

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _to_conversation_response(
    conversation: Conversation,
    *,
    display_name_override: str | None = None,
    last_message_at_override: datetime | None = None,
    last_message_preview: str = "",
) -> ConversationResponse:
    data = serialize_conversation(
        conversation,
        display_name_override=display_name_override,
        last_message_preview=last_message_preview,
    )
    if last_message_at_override is not None:
        data["last_message_at"] = last_message_at_override
    return ConversationResponse.model_validate(data)


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
    from app.application.conversations.whatsapp_conversation_service import (
        conversations_visible_for_tenant,
        ensure_whatsapp_binding_ready,
    )

    if tenant is None or session is None:
        return []

    if ensure_whatsapp_binding_ready(db, tenant=tenant, session=session):
        db.commit()
        db.refresh(session)

    if not conversations_visible_for_tenant(db, tenant=tenant, session=session):
        return []

    from app.application.chatwoot.chatwoot_inbox_sync import sync_chatwoot_inbox
    from app.application.chatwoot.chatwoot_service import (
        chatwoot_sync_mode,
        ensure_chatwoot_integration,
    )

    if chatwoot_sync_mode():
        if ensure_chatwoot_integration(db, tenant=tenant, session=session):
            db.commit()
        sync_chatwoot_inbox(db, tenant=tenant, session=session)

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

    conv_count_pre = query.count()
    if archived is not False and not chatwoot_sync_mode():
        from app.application.sync.sync_scheduler import schedule_whatsapp_sync

        if conv_count_pre == 0:
            schedule_whatsapp_sync(
                current.tenant_id,
                wait_for_history=False,
                delay_seconds=0,
                silent=True,
                debounce=True,
            )
        elif conv_count_pre < 10 and settings.evolution_database_url:
            try:
                from app.infrastructure.evolution.evolution_store import fetch_stored_counts

                evo_chats = fetch_stored_counts(
                    settings.evolution_database_url, session.instance_name
                ).get("chats", 0)
                if evo_chats > conv_count_pre + 5:
                    schedule_whatsapp_sync(
                        current.tenant_id,
                        wait_for_history=False,
                        delay_seconds=0,
                        silent=True,
                        debounce=True,
                    )
            except Exception:
                pass

    last_msg_subq = (
        db.query(
            Message.conversation_id.label("cid"),
            func.max(Message.created_at).label("last_msg_at"),
        )
        .filter(Message.tenant_id == current.tenant_id)
        .group_by(Message.conversation_id)
        .subquery()
    )

    rows = (
        query.outerjoin(last_msg_subq, Conversation.id == last_msg_subq.c.cid)
        .order_by(
            case((last_msg_subq.c.last_msg_at.is_(None), 1), else_=0),
            func.coalesce(last_msg_subq.c.last_msg_at, Conversation.last_message_at)
            .desc()
            .nullslast(),
            Conversation.created_at.desc(),
        )
        .all()
    )
    owner_jid = session.bound_owner_jid or ""
    owner_phone = normalize_phone(session.phone_number or "")
    from app.application.whatsapp.whatsapp_status import build_owner_display_names

    owner_names = build_owner_display_names(session)
    visible = []
    for row in rows:
        if owner_phone and normalize_phone(row.contact_phone) == owner_phone:
            continue
        if is_owner_jid(row.contact_jid or "", owner_jid=owner_jid, owner_phone=owner_phone):
            continue
        if owner_names and is_owner_display_name(row.contact_name, owner_names):
            continue
        visible.append(row)

    mx_map: dict = {}
    preview_map: dict = {}
    if visible:
        conv_ids = [r.id for r in visible]
        mx_rows = (
            db.query(Message.conversation_id, func.max(Message.created_at))
            .filter(Message.conversation_id.in_(conv_ids))
            .group_by(Message.conversation_id)
            .all()
        )
        mx_map = {cid: mx for cid, mx in mx_rows}

        from sqlalchemy import desc as sa_desc

        for conv_id in conv_ids:
            if conv_id not in mx_map:
                continue
            last_msg = (
                db.query(Message.body)
                .filter(Message.conversation_id == conv_id)
                .order_by(sa_desc(Message.created_at))
                .first()
            )
            if last_msg and last_msg[0]:
                preview_map[conv_id] = str(last_msg[0])[:80]

        # Ocultar fantasmas @lid sin mensajes (chats.set vacíos).
        visible = [
            row
            for row in visible
            if row.id in mx_map
            or is_valid_whatsapp_phone(row.contact_phone)
            or not is_placeholder_contact_name(row.contact_name, row.contact_phone)
        ]
        placeholders = [
            row
            for row in visible
            if is_placeholder_contact_name(row.contact_name, row.contact_phone)
        ]
    else:
        placeholders = []

    names_lookup = None
    if (
        not chatwoot_sync_mode()
        and placeholders
        and session.instance_name
        and settings.evolution_database_url
    ):
        from app.application.sync.contact_identity_service import (
            apply_names_lookup_to_conversations,
            build_contact_names_lookup,
        )
        from app.application.sync.contact_name_cache_service import (
            apply_cached_names_to_conversations,
        )

        # Limpiar nombres fantasma guardados por syncs anteriores.
        for row in placeholders:
            if str(row.contact_name or "").strip().lower() == "contacto":
                row.contact_name = ""

        try:
            cache_fixed = apply_cached_names_to_conversations(
                placeholders, session.instance_name, owner_names=owner_names
            )
            if cache_fixed:
                db.commit()
            names_lookup = build_contact_names_lookup(session.instance_name, use_api=True)
            names_fixed = apply_names_lookup_to_conversations(
                placeholders,
                names_lookup,
                owner_names=owner_names,
            )
            if names_fixed:
                db.commit()
        except Exception:
            names_lookup = None

    responses = []
    for row in visible:
        override = None
        if names_lookup is not None and is_placeholder_contact_name(
            row.contact_name, row.contact_phone
        ):
            resolved = names_lookup.resolve_for_conversation(row, owner_names=owner_names)
            if resolved and not is_placeholder_contact_name(resolved, row.contact_phone):
                override = resolved
        responses.append(
            _to_conversation_response(
                row,
                display_name_override=override,
                last_message_at_override=mx_map.get(row.id) or row.last_message_at,
                last_message_preview=preview_map.get(row.id, ""),
            )
        )
    return responses


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


@router.post("/{conversation_id}/sync-live", response_model=ConversationLiveSyncResponse)
def sync_live_conversation(
    conversation_id: uuid.UUID,
    current: RequireViewer,
    db: Session = Depends(get_db),
):
    """Pull mensajes recientes (Chatwoot en modo CW; Evolution solo sin Chatwoot)."""
    from app.domain.entities.enums import WhatsAppStatus
    from app.application.chatwoot.chatwoot_service import chatwoot_sync_mode

    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == current.tenant_id)
        .first()
    )
    if tenant is None or session is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    if tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value:
        raise HTTPException(status_code=409, detail="WhatsApp no conectado")

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

    if chatwoot_sync_mode() and conversation.chatwoot_conversation_id:
        from app.application.chatwoot.chatwoot_inbox_sync import sync_chatwoot_conversation

        imported = sync_chatwoot_conversation(
            db,
            tenant=tenant,
            session=session,
            chatwoot_conversation_id=int(conversation.chatwoot_conversation_id),
        )
        db.commit()
        messages = (
            db.query(Message)
            .filter(
                Message.tenant_id == tenant.id,
                Message.conversation_id == conversation.id,
            )
            .order_by(Message.created_at.asc())
            .all()
        )
        return ConversationLiveSyncResponse(
            imported=imported,
            message_count=len(messages),
            conversation_id=conversation.id,
            messages=[MessageResponse.model_validate(m) for m in messages],
        )

    from app.application.sync.live_sync_service import pull_live_conversation_messages

    imported, messages = pull_live_conversation_messages(
        db,
        tenant=tenant,
        session=session,
        conversation=conversation,
    )
    return ConversationLiveSyncResponse(
        imported=imported,
        message_count=len(messages),
        conversation_id=conversation.id,
        messages=[MessageResponse.model_validate(m) for m in messages],
    )


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
    if body.ai_active:
        from app.application.ai.ai_service import maybe_schedule_ai_for_conversation

        maybe_schedule_ai_for_conversation(
            db,
            tenant_id=current.tenant_id,
            conversation_id=conversation.id,
        )
    return _to_conversation_response(conversation)


@router.post("/{conversation_id}/ai/trigger", status_code=202)
def trigger_conversation_ai(
    conversation_id: uuid.UUID,
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    """Reintenta responder al último mensaje entrante con IA."""
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

    from app.application.ai.ai_service import ai_block_reason, maybe_schedule_ai_for_conversation

    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    reason = ai_block_reason(tenant, conversation)
    if reason:
        raise HTTPException(status_code=409, detail=reason)

    if not maybe_schedule_ai_for_conversation(
        db,
        tenant_id=current.tenant_id,
        conversation_id=conversation.id,
    ):
        raise HTTPException(
            status_code=409,
            detail="No hay mensaje entrante pendiente de respuesta",
        )
    return {"queued": True}


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

    from app.shared.core.phone import phone_to_evolution_number
    from app.infrastructure.evolution.evolution_store import fetch_evolution_message_by_id
    from app.application.messaging.media_cache import get_media_from_cache

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

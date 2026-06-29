from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.shared.core.deps import RequireAgent, RequireOwner
from app.infrastructure.persistence.database import get_db
from app.domain.entities import Tenant, WhatsAppSession
from app.domain.entities.enums import WhatsAppStatus
from app.presentation.schemas.whatsapp import (
    WhatsAppChatsDebugResponse,
    WhatsAppConnectResponse,
    WhatsAppStatusResponse,
    WhatsAppSyncDebugResponse,
    WhatsAppSyncResponse,
)
from app.application.sync.sync_scheduler import (
    ensure_whatsapp_sync_after_connect,
    schedule_whatsapp_sync,
)
from app.application.billing.tenant_service import log_audit
from app.application.whatsapp.whatsapp_service import (
    EvolutionAPIError,
    disconnect_session,
    get_or_create_session,
    reconnect_session,
    refresh_session_status,
    start_connection,
)

log = logging.getLogger(__name__)


router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


def _gateway_unavailable_message(exc: EvolutionAPIError) -> str:
    text = str(exc)
    if "WAHA no responde" in text:
        return (
            "WAHA no responde. Inicia Docker y ejecuta: ./scripts/waha-docker.sh "
            "o cambia WHATSAPP_PROVIDER=evolution en el .env"
        )
    if "Evolution API" in text or "Connection refused" in text or "ConnectError" in text:
        return (
            "Evolution API no está corriendo. En otra terminal ejecuta: "
            "./scripts/evolution-mac.sh start"
        )
    return text


def _gateway_http_error(exc: EvolutionAPIError) -> HTTPException:
    message = _gateway_unavailable_message(exc)
    status_code = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if "no responde" in message.lower() or "no está corriendo" in message.lower()
        else status.HTTP_502_BAD_GATEWAY
    )
    return HTTPException(status_code=status_code, detail=message)


def _session_response(session: WhatsAppSession) -> WhatsAppStatusResponse:
    from app.application.chatwoot.chatwoot_service import chatwoot_panel_url

    return WhatsAppStatusResponse(
        instance_name=session.instance_name,
        status=session.status,
        phone_number=session.phone_number,
        qr_base64=session.qr_base64,
        qr_updated_at=session.qr_updated_at,
        last_connected_at=session.last_connected_at,
        last_disconnected_at=session.last_disconnected_at,
        chatwoot_inbox_url=chatwoot_panel_url(session),
    )


@router.post("/connect", response_model=WhatsAppConnectResponse)
def connect_whatsapp(
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    try:
        session = start_connection(db, tenant)
        log_audit(
            db,
            tenant_id=tenant.id,
            user_id=current.id,
            action="whatsapp.connect_started",
            ip_address=request.client.host if request.client else None,
        )
        db.commit()
        db.refresh(session)
        db.refresh(tenant)
    except EvolutionAPIError as exc:
        db.rollback()
        raise _gateway_http_error(exc) from exc

    if tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value:
        ensure_whatsapp_sync_after_connect(tenant.id, force=True)
        from app.application.whatsapp.whatsapp_service import ensure_evolution_webhook

        ensure_evolution_webhook(session, tenant.id, force=True)

    return WhatsAppConnectResponse(
        instance_name=session.instance_name,
        status=session.status,
        qr_base64=session.qr_base64,
        qr_updated_at=session.qr_updated_at,
        phone_number=session.phone_number,
    )


@router.post("/reconnect", response_model=WhatsAppConnectResponse)
def reconnect_whatsapp(
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    """Regenera QR cuando la sesión cayó (desconectado, restringido o baneado)."""
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    try:
        session = reconnect_session(db, tenant)
        log_audit(
            db,
            tenant_id=tenant.id,
            user_id=current.id,
            action="whatsapp.reconnect_started",
            ip_address=request.client.host if request.client else None,
        )
        db.commit()
        db.refresh(session)
    except EvolutionAPIError as exc:
        db.rollback()
        raise _gateway_http_error(exc) from exc

    return WhatsAppConnectResponse(
        instance_name=session.instance_name,
        status=session.status,
        qr_base64=session.qr_base64,
        qr_updated_at=session.qr_updated_at,
        phone_number=session.phone_number,
    )


@router.get("/status", response_model=WhatsAppStatusResponse)
def whatsapp_status(current: RequireAgent, db: Session = Depends(get_db)):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    session = get_or_create_session(db, tenant)
    binding_repaired = False
    try:
        refresh = refresh_session_status(db, tenant, session)
        session = refresh.session
        if tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value and session.active_connection_id is None:
            from app.application.conversations.whatsapp_conversation_service import (
                ensure_whatsapp_binding_ready,
            )

            binding_repaired = ensure_whatsapp_binding_ready(
                db,
                tenant=tenant,
                session=session,
            )
        db.commit()
        db.refresh(session)
        db.refresh(tenant)
    except EvolutionAPIError:
        db.rollback()
        session = get_or_create_session(db, tenant)
        refresh = None

    if refresh and refresh.should_sync:
        ensure_whatsapp_sync_after_connect(tenant.id, force=True)
    elif binding_repaired:
        ensure_whatsapp_sync_after_connect(tenant.id, force=True)
    elif tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value and session.active_connection_id:
        from app.domain.entities import Conversation
        from app.infrastructure.cache.redis_client import get_redis

        conv_count = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant.id,
                Conversation.whatsapp_connection_id == session.active_connection_id,
            )
            .count()
        )
        boot_key = f"tenant:{tenant.id}:auto_sync:{session.active_connection_id}"
        if conv_count == 0 and get_redis().set(boot_key, "1", nx=True, ex=600):
            ensure_whatsapp_sync_after_connect(tenant.id, force=True)

    if tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value:
        from app.application.whatsapp.whatsapp_service import ensure_evolution_webhook

        ensure_evolution_webhook(session, tenant.id, force=False)

    return _session_response(session)


@router.post("/sync", response_model=WhatsAppSyncResponse)
def sync_whatsapp_chats_endpoint(
    request: Request,
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    """Importa chats del celular en background (no bloquea el panel)."""
    from app.infrastructure.cache.redis_client import get_redis
    from app.domain.entities.enums import WhatsAppStatus

    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == current.tenant_id)
        .first()
    )
    if tenant is None or session is None:
        raise HTTPException(status_code=404, detail="Sesión WhatsApp no encontrada")

    if tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value:
        raise HTTPException(
            status_code=409,
            detail="WhatsApp no conectado — escanea el QR antes de sincronizar",
        )

    from app.application.chatwoot.chatwoot_service import chatwoot_sync_mode

    sync_label = (
        "Sincronizando chats desde Chatwoot…"
        if chatwoot_sync_mode()
        else "Sincronizando chats del celular…"
    )

    started = schedule_whatsapp_sync(
        tenant.id,
        user_id=current.id,
        ip_address=request.client.host if request.client else None,
        wait_for_history=False,
        delay_seconds=0,
    )
    if not started:
        return WhatsAppSyncResponse(
            status="running",
            message=sync_label,
        )

    return WhatsAppSyncResponse(
        status="started",
        message=sync_label,
    )


@router.get("/debug/chats", response_model=WhatsAppChatsDebugResponse)
def debug_whatsapp_chats(
    current: RequireAgent,
    db: Session = Depends(get_db),
    sample_limit: int = 25,
):
    """Diagnóstico de importación de chats y resolución de nombres (para soporte)."""
    from app.application.sync.chat_debug_service import build_chats_debug_report

    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == current.tenant_id)
        .first()
    )
    if tenant is None or session is None:
        raise HTTPException(status_code=404, detail="Sesión WhatsApp no encontrada")

    report = build_chats_debug_report(
        db,
        tenant=tenant,
        session=session,
        sample_limit=max(5, min(sample_limit, 50)),
    )
    return WhatsAppChatsDebugResponse.model_validate(report)


@router.get("/debug/sync", response_model=WhatsAppSyncDebugResponse)
def debug_whatsapp_sync(
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    """Diagnóstico de sincronización en tiempo real (webhooks, cola, mensajes)."""
    from app.application.sync.sync_debug_service import build_sync_debug_report

    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == current.tenant_id)
        .first()
    )
    if tenant is None or session is None:
        raise HTTPException(status_code=404, detail="Sesión WhatsApp no encontrada")

    report = build_sync_debug_report(db, tenant=tenant, session=session)
    return WhatsAppSyncDebugResponse.model_validate(report)


@router.post("/enrich-contacts", response_model=WhatsAppSyncResponse)
def enrich_whatsapp_contacts(
    request: Request,
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    """Repara nombres lid:… y teléfonos sin reiniciar Evolution (~5 s)."""
    from app.domain.entities.enums import WhatsAppStatus
    from app.application.realtime.realtime_service import publish_panel_event

    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == current.tenant_id)
        .first()
    )
    if tenant is None or session is None:
        raise HTTPException(status_code=404, detail="Sesión WhatsApp no encontrada")

    if tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value:
        raise HTTPException(
            status_code=409,
            detail="WhatsApp no conectado — escanea el QR antes de actualizar contactos",
        )

    try:
        refresh = refresh_session_status(db, tenant, session)
        session = refresh.session
        db.commit()
    except EvolutionAPIError:
        db.rollback()

    from app.infrastructure.cache.redis_client import cache_delete, tenant_cache_key

    if session.instance_name:
        cache_delete(tenant_cache_key(session.instance_name, "owner_display_names"))

    from app.application.whatsapp.whatsapp_status import build_owner_display_names

    owner_names = build_owner_display_names(session)
    from app.application.chatwoot.chatwoot_service import (
        chatwoot_sync_mode,
        ensure_chatwoot_integration,
    )

    if chatwoot_sync_mode():
        ensure_chatwoot_integration(db, tenant=tenant, session=session)
        from app.application.chatwoot.chatwoot_inbox_sync import sync_chatwoot_inbox

        stats = sync_chatwoot_inbox(db, tenant=tenant, session=session)
        log_audit(
            db,
            tenant_id=tenant.id,
            user_id=current.id,
            action="whatsapp.contacts_enriched",
            details=stats,
            ip_address=request.client.host if request.client else None,
        )
        db.commit()
        publish_panel_event(
            tenant.id,
            {"type": "sync.completed", "source": "chatwoot", **stats, "status": "completed"},
        )
        return WhatsAppSyncResponse(
            status="completed",
            message=(
                f"Sincronizado desde Chatwoot: {stats.get('conversations', 0)} chats, "
                f"{stats.get('messages', 0)} mensajes."
            ),
        )

    from app.application.sync.contact_identity_service import enrich_tenant_conversations
    from app.application.sync.contact_name_cache_service import (
        backfill_names_from_evolution_api,
        backfill_names_from_evolution_db,
    )

    backfill_names_from_evolution_db(session.instance_name, owner_names=owner_names)
    backfill_names_from_evolution_api(
        session.instance_name, max_pages=15, owner_names=owner_names
    )
    stats = enrich_tenant_conversations(db, tenant=tenant, session=session)
    log_audit(
        db,
        tenant_id=tenant.id,
        user_id=current.id,
        action="whatsapp.contacts_enriched",
        details=stats,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()

    from app.domain.entities import Conversation
    from app.application.realtime.realtime_service import publish_conversation_updated

    rows = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant.id,
            Conversation.whatsapp_connection_id == session.active_connection_id,
        )
        .all()
    )
    for row in rows:
        publish_conversation_updated(tenant.id, row)

    publish_panel_event(
        tenant.id,
        {"type": "contacts.enriched", **stats, "status": "completed"},
    )

    return WhatsAppSyncResponse(
        status="completed",
        contacts_enriched=stats["enriched"],
        names_fixed=stats["names_fixed"],
        phones_fixed=stats["phones_fixed"],
        profile_fetched=stats.get("profile_fetched", 0),
        profile_names_fixed=stats.get("profile_names_fixed", 0),
        message=(
            f"Contactos actualizados: {stats['names_fixed']} nombres, "
            f"{stats['phones_fixed']} teléfonos reparados."
            + (
                f" Chats fusionados: {stats.get('conversations_merged', 0)}."
                if stats.get("conversations_merged")
                else ""
            )
            + (
                f" Perfiles WA: {stats.get('profile_names_fixed', 0)} de "
                f"{stats.get('profile_fetched', 0)} consultados."
                if stats.get("profile_fetched")
                else ""
            )
        ),
    )


@router.post("/reset-binding", response_model=WhatsAppStatusResponse)
def reset_whatsapp_binding(
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    """Borra chats de otro celular y empieza vinculación limpia (sin desconectar Evolution)."""
    from app.domain.entities.enums import WhatsAppStatus
    from app.application.conversations.whatsapp_conversation_service import start_new_whatsapp_binding

    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == current.tenant_id)
        .first()
    )
    if tenant is None or session is None:
        raise HTTPException(status_code=404, detail="Sesión WhatsApp no encontrada")

    if tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value:
        raise HTTPException(
            status_code=409,
            detail="WhatsApp no conectado — escanea el QR primero",
        )

    start_new_whatsapp_binding(
        db,
        tenant=tenant,
        session=session,
        instance_name=session.instance_name,
    )
    log_audit(
        db,
        tenant_id=tenant.id,
        user_id=current.id,
        action="whatsapp.binding_reset",
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(session)
    ensure_whatsapp_sync_after_connect(tenant.id, force=True)
    return _session_response(session)


@router.post("/disconnect", status_code=status.HTTP_204_NO_CONTENT)
def disconnect_whatsapp(
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == current.tenant_id)
        .first()
    )
    if tenant is None or session is None:
        raise HTTPException(status_code=404, detail="Sesión WhatsApp no encontrada")

    disconnect_session(db, tenant, session)
    from app.application.realtime.realtime_service import publish_whatsapp_status

    publish_whatsapp_status(
        tenant.id,
        status=session.status,
        qr_base64=None,
        phone_number=session.phone_number,
    )
    log_audit(
        db,
        tenant_id=tenant.id,
        user_id=current.id,
        action="whatsapp.disconnected",
        ip_address=request.client.host if request.client else None,
    )
    db.commit()

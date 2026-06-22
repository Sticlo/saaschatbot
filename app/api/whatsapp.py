from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.deps import RequireAgent, RequireOwner
from app.database import get_db
from app.models import Tenant, WhatsAppSession
from app.models.enums import WhatsAppStatus
from app.schemas.whatsapp import (
    WhatsAppConnectResponse,
    WhatsAppStatusResponse,
    WhatsAppSyncResponse,
)
from app.services.sync_scheduler import (
    ensure_whatsapp_sync_after_connect,
    schedule_whatsapp_sync,
)
from app.services.contact_identity_service import enrich_tenant_conversations
from app.services.evolution_client import EvolutionAPIError
from app.services.tenant_service import log_audit
from app.services.whatsapp_service import (
    disconnect_session,
    get_or_create_session,
    reconnect_session,
    refresh_session_status,
    start_connection,
)

log = logging.getLogger(__name__)


router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


def _session_response(session: WhatsAppSession) -> WhatsAppStatusResponse:
    return WhatsAppStatusResponse(
        instance_name=session.instance_name,
        status=session.status,
        phone_number=session.phone_number,
        qr_base64=session.qr_base64,
        qr_updated_at=session.qr_updated_at,
        last_connected_at=session.last_connected_at,
        last_disconnected_at=session.last_disconnected_at,
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
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    if tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value:
        ensure_whatsapp_sync_after_connect(tenant.id, force=True)

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
        raise HTTPException(status_code=502, detail=str(exc)) from exc

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
    try:
        refresh = refresh_session_status(db, tenant, session)
        session = refresh.session
        db.commit()
        db.refresh(session)
        db.refresh(tenant)
    except EvolutionAPIError:
        db.rollback()
        session = get_or_create_session(db, tenant)
        refresh = None

    if refresh and refresh.should_sync:
        ensure_whatsapp_sync_after_connect(tenant.id, force=True)

    return _session_response(session)


@router.post("/sync", response_model=WhatsAppSyncResponse)
def sync_whatsapp_chats_endpoint(
    request: Request,
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    """Importa chats del celular en background (no bloquea el panel)."""
    from app.redis_client import get_redis
    from app.models.enums import WhatsAppStatus

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

    started = schedule_whatsapp_sync(
        tenant.id,
        user_id=current.id,
        ip_address=request.client.host if request.client else None,
        wait_for_history=True,
        delay_seconds=1,
    )
    if not started:
        return WhatsAppSyncResponse(
            status="running",
            message="Sincronizando chats del celular…",
        )

    return WhatsAppSyncResponse(
        status="started",
        message="Sincronizando chats del celular…",
    )


@router.post("/enrich-contacts", response_model=WhatsAppSyncResponse)
def enrich_whatsapp_contacts(
    request: Request,
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    """Repara nombres lid:… y teléfonos sin reiniciar Evolution (~5 s)."""
    from app.models.enums import WhatsAppStatus
    from app.services.realtime_service import publish_panel_event

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

    from app.models import Conversation
    from app.services.realtime_service import publish_conversation_updated

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
        message=(
            f"Contactos actualizados: {stats['names_fixed']} nombres, "
            f"{stats['phones_fixed']} teléfonos reparados."
        ),
    )


@router.post("/reset-binding", response_model=WhatsAppStatusResponse)
def reset_whatsapp_binding(
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    """Borra chats de otro celular y empieza vinculación limpia (sin desconectar Evolution)."""
    from app.models.enums import WhatsAppStatus
    from app.services.whatsapp_conversation_service import start_new_whatsapp_binding

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
    from app.services.realtime_service import publish_whatsapp_status

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

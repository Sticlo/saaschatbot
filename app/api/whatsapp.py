from __future__ import annotations

import logging
import threading
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.deps import RequireAgent, RequireOwner
from app.database import get_db
from app.models import Tenant, WhatsAppSession
from app.schemas.whatsapp import (
    WhatsAppConnectResponse,
    WhatsAppStatusResponse,
    WhatsAppSyncResponse,
)
from app.services.chat_sync_service import sync_whatsapp_chats
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


def _run_sync_job(tenant_id: uuid.UUID, user_id: uuid.UUID, ip_address) -> None:
    from app.database import SessionLocal
    from app.redis_client import get_redis
    from app.services.realtime_service import publish_panel_event

    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()
        if tenant is None or session is None:
            return
        stats = sync_whatsapp_chats(db, tenant=tenant, session=session)
        log_audit(
            db,
            tenant_id=tenant.id,
            user_id=user_id,
            action="whatsapp.chats_synced",
            details=stats,
            ip_address=ip_address,
        )
        db.commit()
        publish_panel_event(
            tenant_id,
            {"type": "sync.completed", **stats, "status": "completed"},
        )
    except Exception as exc:
        log.warning("Sync background falló tenant=%s: %s", tenant_id, exc)
        db.rollback()
        try:
            publish_panel_event(
                tenant_id,
                {"type": "sync.completed", "status": "failed", "message": str(exc)},
            )
        except Exception:
            pass
    finally:
        db.close()
        try:
            get_redis().delete(f"tenant:{tenant_id}:sync_running")
        except Exception:
            pass

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
    except EvolutionAPIError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

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
        session = refresh_session_status(db, tenant, session)
        db.commit()
        db.refresh(session)
    except EvolutionAPIError:
        db.rollback()
        session = get_or_create_session(db, tenant)

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

    lock_key = f"tenant:{tenant.id}:sync_running"
    redis = get_redis()
    if not redis.set(lock_key, "1", nx=True, ex=120):
        return WhatsAppSyncResponse(
            status="running",
            message="Ya hay una sincronización en curso. Espera un momento y pulsa ↻",
        )

    threading.Thread(
        target=_run_sync_job,
        args=(tenant.id, current.id, request.client.host if request.client else None),
        daemon=True,
    ).start()

    return WhatsAppSyncResponse(
        status="started",
        message=(
            "Sincronizando chats del celular… reinicia Evolution y espera hasta 90 s "
            "mientras WhatsApp envía el historial. Te avisamos cuando termine."
        ),
    )


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

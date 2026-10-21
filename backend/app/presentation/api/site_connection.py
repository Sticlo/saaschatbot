from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.application.availability import site_connector
from app.application.availability.availability_service import CalendarError
from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.application.billing.tenant_service import log_audit
from app.config import settings
from app.infrastructure.persistence.database import get_db
from app.shared.core.deps import RequireOwner
from app.shared.core.rate_limit import enforce_rate_limit

router = APIRouter(prefix="/site-connection", tags=["site-connection"])


class ConnectBody(BaseModel):
    url: str = Field(max_length=1000)


class ToolsBody(BaseModel):
    allowed: list[str] = Field(default_factory=list, max_length=site_connector.MAX_TOOLS)


@router.get("")
def get_connection(current: RequireOwner, db: Session = Depends(get_db)):
    profile = get_or_create_tenant_profile(db, current.tenant_id)
    db.commit()
    return site_connector.public_view(profile.site_connection)


@router.post("/start")
def start(body: ConnectBody, current: RequireOwner):
    enforce_rate_limit(
        f"siteconn:start:{current.tenant_id}",
        limit=10,
        window_seconds=3600,
        message="Intentaste conectar muchas veces seguidas. Espera un rato.",
    )
    try:
        url = site_connector.start_connection(current.tenant_id, current.id, body.url)
    except CalendarError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"authorize_url": url}


@router.get("/callback", include_in_schema=False)
def callback(request: Request, state: str = "", code: str = "", error: str = "", db: Session = Depends(get_db)):
    """El dueño vuelve aquí desde su web después de autorizar. El state lo ata a su negocio."""
    panel = f"{settings.panel_base_url()}/panel"
    if error or not code:
        return RedirectResponse(f"{panel}?site=error&msg={quote('No autorizaste la conexión.')}", status_code=303)
    try:
        tenant_id = site_connector.finish_connection(db, state, code)
    except CalendarError as exc:
        db.rollback()
        return RedirectResponse(f"{panel}?site=error&msg={quote(str(exc))}", status_code=303)
    log_audit(
        db,
        tenant_id=tenant_id,
        user_id=None,
        action="site_connection.connected",
        details={"server_url": (get_or_create_tenant_profile(db, tenant_id).site_connection or {}).get("server_url")},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return RedirectResponse(f"{panel}?site=ok", status_code=303)


@router.post("/refresh")
def refresh(current: RequireOwner):
    enforce_rate_limit(
        f"siteconn:refresh-tools:{current.tenant_id}",
        limit=20,
        window_seconds=3600,
        message="Actualizaste muchas veces seguidas. Espera un rato.",
    )
    try:
        conn = site_connector.refresh_tools(current.tenant_id)
    except CalendarError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return site_connector.public_view(conn)


@router.put("/tools")
def set_tools(body: ToolsBody, request: Request, current: RequireOwner, db: Session = Depends(get_db)):
    try:
        conn = site_connector.set_allowed_tools(db, current.tenant_id, body.allowed)
    except CalendarError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="site_connection.tools_updated",
        details={"allowed": conn.get("allowed_tools") or []},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return site_connector.public_view(conn)


@router.delete("")
def disconnect(request: Request, current: RequireOwner, db: Session = Depends(get_db)):
    site_connector.disconnect(db, current.tenant_id)
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="site_connection.disconnected",
        details={},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return {"connected": False}

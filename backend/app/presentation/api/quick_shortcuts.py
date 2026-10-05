from __future__ import annotations

import base64
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.application.billing.tenant_service import log_audit
from app.application.outbound.quick_shortcut_service import get_shortcuts, save_shortcuts
from app.application.outbound.tenant_asset_service import (
    PDF_MIME,
    display_file_name,
    resolve_asset_path,
    save_tenant_catalog_file,
)
from app.application.platform.tenant_overrides import feature_allowed
from app.domain.entities import Tenant
from app.infrastructure.ai import gemini_client
from app.infrastructure.persistence.database import get_db
from app.presentation.schemas.quick_shortcuts import (
    CatalogFileUploadResponse,
    QuickShortcutResponse,
    QuickShortcutsUpdate,
)
from app.shared.core.deps import RequireOwner, RequireViewer
from app.shared.core.rate_limit import enforce_rate_limit

log = logging.getLogger(__name__)

router = APIRouter(prefix="/quick-shortcuts", tags=["quick-shortcuts"])


def _asset_url(path: str | None) -> str | None:
    return f"/api/v1/outbound/assets/{path.split('/')[-1]}" if path else None


def _to_response(item: dict) -> QuickShortcutResponse:
    return QuickShortcutResponse(
        id=str(item["id"]),
        label=item["label"],
        type=item["type"],
        text=item.get("text"),
        image_path=item.get("image_path"),
        image_url=_asset_url(item.get("image_path")),
        file_path=item.get("file_path"),
        file_name=item.get("file_name"),
        file_url=_asset_url(item.get("file_path")),
        content=item.get("content"),
    )


@router.get("", response_model=list[QuickShortcutResponse])
def list_shortcuts(current: RequireViewer, db: Session = Depends(get_db)):
    rows = get_shortcuts(db, current.tenant_id)
    db.commit()
    return [_to_response(r) for r in rows]


@router.post("/files", response_model=CatalogFileUploadResponse)
def upload_catalog_file(
    current: RequireOwner, file: UploadFile = File(...), db: Session = Depends(get_db)
):
    """Sube una foto o PDF (menú, carta, catálogo) y deja que la IA lea qué contiene."""
    if not feature_allowed(db.get(Tenant, current.tenant_id), "catalog_files"):
        raise HTTPException(status_code=403, detail="Tu plan no incluye subir catálogos. Escríbenos para activarlo.")
    enforce_rate_limit(
        f"shortcuts:file:{current.tenant_id}",
        limit=30,
        window_seconds=3600,
        message="Subiste muchos archivos seguidos. Intenta de nuevo en un rato.",
    )
    path, mime = save_tenant_catalog_file(current.tenant_id, file)
    is_pdf = mime == PDF_MIME
    content = ""
    if gemini_client.is_configured():
        try:
            raw = resolve_asset_path(path, tenant_id=current.tenant_id).read_bytes()
            content = gemini_client.extract_catalog(base64.b64encode(raw).decode("ascii"), mime)
        except gemini_client.GeminiError as exc:
            log.warning("No se pudo leer el catálogo tenant=%s: %s", current.tenant_id, exc)
    return CatalogFileUploadResponse(
        type="document" if is_pdf else "image",
        path=path,
        url=_asset_url(path) or "",
        file_name=display_file_name(file.filename or "") if is_pdf else None,
        content=content,
        content_ok=bool(content),
    )


@router.put("", response_model=list[QuickShortcutResponse])
def update_shortcuts(
    body: QuickShortcutsUpdate,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    tenant_prefix = f"assets/{current.tenant_id}/"
    payload = []
    for s in body.shortcuts:
        item = s.model_dump()
        for key in ("image_path", "file_path"):
            if item.get(key) and not item[key].startswith(tenant_prefix):
                raise HTTPException(status_code=400, detail="Archivo no válido")
        if item.get("file_name"):
            item["file_name"] = display_file_name(item["file_name"])
        payload.append(item)
    try:
        rows = save_shortcuts(db, current.tenant_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="quick_shortcuts.updated",
        details={"count": len(rows)},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return [_to_response(r) for r in rows]

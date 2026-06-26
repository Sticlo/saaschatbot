from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.application.billing.tenant_service import log_audit
from app.application.outbound.quick_shortcut_service import get_shortcuts, save_shortcuts
from app.infrastructure.persistence.database import get_db
from app.presentation.schemas.quick_shortcuts import (
    QuickShortcutResponse,
    QuickShortcutsUpdate,
)
from app.shared.core.deps import RequireOwner, RequireViewer

router = APIRouter(prefix="/quick-shortcuts", tags=["quick-shortcuts"])


def _to_response(item: dict) -> QuickShortcutResponse:
    image_path = item.get("image_path")
    image_url = None
    if image_path:
        image_url = f"/api/v1/outbound/assets/{image_path.split('/')[-1]}"
    return QuickShortcutResponse(
        id=str(item["id"]),
        label=item["label"],
        type=item["type"],
        text=item.get("text"),
        image_path=image_path,
        image_url=image_url,
    )


@router.get("", response_model=list[QuickShortcutResponse])
def list_shortcuts(current: RequireViewer, db: Session = Depends(get_db)):
    rows = get_shortcuts(db, current.tenant_id)
    return [_to_response(r) for r in rows]


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
        if item.get("type") == "image" and item.get("image_path"):
            if not item["image_path"].startswith(tenant_prefix):
                raise HTTPException(status_code=400, detail="Imagen no válida")
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

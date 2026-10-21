from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.application.ai.ai_flow_service import MAX_FLOWS, FlowError, get_flows, save_flows
from app.application.billing.tenant_service import log_audit
from app.application.outbound.quick_shortcut_service import get_shortcuts
from app.infrastructure.persistence.database import get_db
from app.shared.core.deps import RequireOwner, RequireViewer

router = APIRouter(prefix="/ai-flows", tags=["ai-flows"])


class FlowsBody(BaseModel):
    flows: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_FLOWS)


@router.get("")
def list_flows(current: RequireViewer, db: Session = Depends(get_db)):
    return {"flows": get_flows(db, current.tenant_id)}


@router.put("")
def update_flows(body: FlowsBody, request: Request, current: RequireOwner, db: Session = Depends(get_db)):
    shortcut_ids = {str(s.get("id")) for s in get_shortcuts(db, current.tenant_id)}
    try:
        flows = save_flows(db, current.tenant_id, body.flows, shortcut_ids)
    except FlowError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="ai_flows.updated",
        details={"count": len(flows), "active": sum(1 for f in flows if f["active"])},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return {"flows": flows}

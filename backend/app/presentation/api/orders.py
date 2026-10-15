from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.application.billing.tenant_service import log_audit
from app.application.orders.order_service import (
    STATUSES,
    OrderError,
    get_order,
    list_orders,
    order_to_dict,
    set_status,
)
from app.application.realtime.realtime_service import publish_panel_event
from app.domain.entities import Tenant
from app.infrastructure.persistence.database import get_db
from app.shared.core.deps import RequireAgent, RequireViewer

router = APIRouter(prefix="/orders", tags=["orders"])


class OrderUpdate(BaseModel):
    status: str = Field(pattern="^(" + "|".join(STATUSES) + ")$")
    tracking: str = Field(default="", max_length=120)
    notify_customer: bool = True


class OrderResponse(BaseModel):
    id: str
    conversation_id: Optional[str] = None
    contact_name: str = ""
    contact_phone: str = ""
    items: str
    total: str = ""
    customer: str = ""
    delivery: str = ""
    payment: str = ""
    payment_proof: bool = False
    status: str
    tracking: str = ""
    dispatched_at: Optional[str] = None
    created_at: Optional[str] = None
    customer_notified: bool = False


@router.get("", response_model=list[OrderResponse])
def get_orders(current: RequireViewer, db: Session = Depends(get_db)):
    return [OrderResponse(**order_to_dict(o)) for o in list_orders(db, current.tenant_id)]


@router.patch("/{order_id}", response_model=OrderResponse)
def patch_order(order_id: uuid.UUID, body: OrderUpdate, current: RequireAgent, db: Session = Depends(get_db)):
    tenant = db.query(Tenant).filter(Tenant.id == current.tenant_id).first()
    try:
        order = get_order(db, current.tenant_id, order_id)
        notified = set_status(
            db, tenant, order, body.status, tracking=body.tracking, notify_customer=body.notify_customer
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action=f"order.{body.status}",
        details={"order_id": str(order.id), "tracking": order.tracking or None},
    )
    db.commit()
    db.refresh(order)
    publish_panel_event(current.tenant_id, {"type": "orders.updated"})
    return OrderResponse(**order_to_dict(order), customer_notified=notified)

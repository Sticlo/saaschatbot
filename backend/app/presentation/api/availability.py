from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.application.availability.availability_service import (
    MAX_CALENDARS,
    CalendarError,
    check_calendar,
    get_calendars,
    save_calendars,
)
from app.application.billing.tenant_service import log_audit
from app.infrastructure.persistence.database import get_db
from app.shared.core.deps import RequireOwner
from app.shared.core.rate_limit import enforce_rate_limit

router = APIRouter(prefix="/availability", tags=["availability"])


class CalendarItem(BaseModel):
    id: str | None = None
    name: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=2000)


class CalendarsUpdate(BaseModel):
    calendars: list[CalendarItem] = Field(default_factory=list, max_length=MAX_CALENDARS)


class CalendarCheck(BaseModel):
    url: str = Field(max_length=2000)


# El enlace iCal deja ver las reservas: solo el dueño lo ve y lo cambia.
@router.get("/calendars")
def list_calendars(current: RequireOwner, db: Session = Depends(get_db)):
    rows = get_calendars(db, current.tenant_id)
    db.commit()
    return rows


@router.put("/calendars")
def update_calendars(body: CalendarsUpdate, request: Request, current: RequireOwner, db: Session = Depends(get_db)):
    try:
        rows = save_calendars(db, current.tenant_id, [c.model_dump() for c in body.calendars])
    except CalendarError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="availability.calendars_updated",
        details={"count": len(rows), "names": [r["name"] for r in rows]},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return rows


@router.post("/calendars/check")
def check_calendar_link(body: CalendarCheck, current: RequireOwner):
    enforce_rate_limit(
        f"availability:check:{current.tenant_id}",
        limit=30,
        window_seconds=3600,
        message="Revisaste muchos calendarios seguidos. Intenta de nuevo en un rato.",
    )
    try:
        busy = check_calendar(body.url)
    except CalendarError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "busy_days": busy}

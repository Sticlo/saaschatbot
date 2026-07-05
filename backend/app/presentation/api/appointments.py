from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.application.appointments.appointment_service import (
    build_day_view,
    create_appointment,
    delete_appointment,
    get_schedule,
    parse_slot_to_datetimes,
    update_appointment,
    update_schedule,
)
from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.application.billing.tenant_service import log_audit
from app.infrastructure.persistence.database import get_db
from app.presentation.schemas.appointments import (
    AppointmentCreateRequest,
    AppointmentDayResponse,
    AppointmentResponse,
    AppointmentScheduleResponse,
    AppointmentScheduleUpdate,
    AppointmentUpdateRequest,
    _parse_day,
)
from app.shared.core.deps import RequireAgent, RequireOwner, RequireViewer

router = APIRouter(prefix="/appointments", tags=["appointments"])


def _to_response(row) -> AppointmentResponse:
    return AppointmentResponse(
        id=str(row.id),
        conversation_id=str(row.conversation_id) if row.conversation_id else None,
        starts_at=row.starts_at.isoformat(),
        ends_at=row.ends_at.isoformat(),
        client_name=row.client_name,
        client_phone=row.client_phone,
        notes=row.notes or "",
    )


@router.get("/schedule", response_model=AppointmentScheduleResponse)
def get_appointment_schedule(current: RequireViewer, db: Session = Depends(get_db)):
    profile = get_or_create_tenant_profile(db, current.tenant_id)
    db.commit()
    schedule = get_schedule(profile)
    return AppointmentScheduleResponse(
        open_time=schedule.open_time,
        close_time=schedule.close_time,
        slot_minutes=schedule.slot_minutes,
    )


@router.put("/schedule", response_model=AppointmentScheduleResponse)
def put_appointment_schedule(
    body: AppointmentScheduleUpdate,
    request: Request,
    current: RequireOwner,
    db: Session = Depends(get_db),
):
    profile = get_or_create_tenant_profile(db, current.tenant_id)
    try:
        schedule = update_schedule(
            profile,
            open_time=body.open_time,
            close_time=body.close_time,
            slot_minutes=body.slot_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="appointments.schedule_updated",
        details={
            "open_time": schedule.open_time,
            "close_time": schedule.close_time,
            "slot_minutes": schedule.slot_minutes,
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return AppointmentScheduleResponse(
        open_time=schedule.open_time,
        close_time=schedule.close_time,
        slot_minutes=schedule.slot_minutes,
    )


@router.get("/day", response_model=AppointmentDayResponse)
def get_appointment_day(
    current: RequireViewer,
    db: Session = Depends(get_db),
    day: str = Query(..., description="YYYY-MM-DD"),
):
    try:
        parsed_day = _parse_day(day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = build_day_view(db, tenant_id=current.tenant_id, day=parsed_day)
    db.commit()
    return AppointmentDayResponse.model_validate(payload)


@router.post("", response_model=AppointmentResponse, status_code=201)
def post_appointment(
    body: AppointmentCreateRequest,
    request: Request,
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    try:
        parsed_day = _parse_day(body.date)
        starts_at, ends_at = parse_slot_to_datetimes(parsed_day, body.start_time, body.end_time)
        row = create_appointment(
            db,
            tenant_id=current.tenant_id,
            starts_at=starts_at,
            ends_at=ends_at,
            client_name=body.client_name,
            client_phone=body.client_phone,
            notes=body.notes,
            conversation_id=body.conversation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="appointments.created",
        details={"appointment_id": str(row.id), "client_name": row.client_name},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(row)
    return _to_response(row)


@router.patch("/{appointment_id}", response_model=AppointmentResponse)
def patch_appointment(
    appointment_id: uuid.UUID,
    body: AppointmentUpdateRequest,
    request: Request,
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    starts_at = None
    ends_at = None
    if body.date and body.start_time and body.end_time:
        try:
            parsed_day = _parse_day(body.date)
            starts_at, ends_at = parse_slot_to_datetimes(
                parsed_day, body.start_time, body.end_time
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif any([body.date, body.start_time, body.end_time]):
        raise HTTPException(
            status_code=400,
            detail="Para cambiar horario envía date, start_time y end_time",
        )

    try:
        row = update_appointment(
            db,
            tenant_id=current.tenant_id,
            appointment_id=appointment_id,
            client_name=body.client_name,
            client_phone=body.client_phone,
            notes=body.notes,
            starts_at=starts_at,
            ends_at=ends_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="appointments.updated",
        details={"appointment_id": str(row.id)},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(row)
    return _to_response(row)


@router.delete("/{appointment_id}", status_code=204)
def remove_appointment(
    appointment_id: uuid.UUID,
    request: Request,
    current: RequireAgent,
    db: Session = Depends(get_db),
):
    try:
        delete_appointment(db, tenant_id=current.tenant_id, appointment_id=appointment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="appointments.deleted",
        details={"appointment_id": str(appointment_id)},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()

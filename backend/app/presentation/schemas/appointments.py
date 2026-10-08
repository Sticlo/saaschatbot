from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class AppointmentScheduleResponse(BaseModel):
    open_time: str
    close_time: str
    slot_minutes: int
    ai_booking_enabled: bool = False
    ai_booking_allowed: bool = True
    staff_label: str = ""


class AppointmentScheduleUpdate(BaseModel):
    open_time: str = Field(min_length=4, max_length=5)
    close_time: str = Field(min_length=4, max_length=5)
    slot_minutes: int = Field(ge=15, le=240)
    ai_booking_enabled: Optional[bool] = None
    staff_label: Optional[str] = Field(default=None, max_length=40)


class AppointmentSlotAppointment(BaseModel):
    id: str
    conversation_id: Optional[str] = None
    staff_id: Optional[str] = None
    staff_name: str = ""
    starts_at: str
    ends_at: str
    client_name: str
    client_phone: Optional[str] = None
    notes: str = ""


class AppointmentDaySlot(BaseModel):
    start: str
    end: str
    status: str
    appointment: Optional[AppointmentSlotAppointment] = None


class StaffMemberResponse(BaseModel):
    id: str
    name: str
    phone: str = ""
    is_active: bool = True
    work_days: list[int]
    start_time: str = ""
    end_time: str = ""
    upcoming_count: int = 0


class StaffMemberRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: Optional[str] = Field(default=None, max_length=32)
    work_days: list[int] = Field(min_length=1, max_length=7)
    start_time: Optional[str] = Field(default=None, max_length=5)
    end_time: Optional[str] = Field(default=None, max_length=5)
    is_active: bool = True


class AppointmentDayResponse(BaseModel):
    date: str
    schedule: AppointmentScheduleResponse
    staff: list[StaffMemberResponse] = []
    staff_id: Optional[str] = None
    off_day: bool = False
    slots: list[AppointmentDaySlot]


class AppointmentCreateRequest(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    start_time: str = Field(min_length=4, max_length=5)
    end_time: str = Field(min_length=4, max_length=5)
    client_name: str = Field(min_length=1, max_length=255)
    client_phone: Optional[str] = Field(default=None, max_length=64)
    notes: Optional[str] = Field(default=None, max_length=2000)
    conversation_id: Optional[uuid.UUID] = None
    staff_id: Optional[uuid.UUID] = Field(default=None, description="Vacío = quien esté libre")


class AppointmentUpdateRequest(BaseModel):
    date: Optional[str] = None
    start_time: Optional[str] = Field(default=None, min_length=4, max_length=5)
    end_time: Optional[str] = Field(default=None, min_length=4, max_length=5)
    client_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    client_phone: Optional[str] = Field(default=None, max_length=64)
    notes: Optional[str] = Field(default=None, max_length=2000)
    staff_id: Optional[uuid.UUID] = None


class AppointmentResponse(BaseModel):
    id: str
    conversation_id: Optional[str] = None
    staff_id: Optional[str] = None
    staff_name: str = ""
    starts_at: str
    ends_at: str
    client_name: str
    client_phone: Optional[str] = None
    notes: str = ""


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError("Fecha inválida (usa YYYY-MM-DD)") from exc

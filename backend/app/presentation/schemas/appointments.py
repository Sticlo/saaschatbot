from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class AppointmentScheduleResponse(BaseModel):
    open_time: str
    close_time: str
    slot_minutes: int


class AppointmentScheduleUpdate(BaseModel):
    open_time: str = Field(min_length=4, max_length=5)
    close_time: str = Field(min_length=4, max_length=5)
    slot_minutes: int = Field(ge=15, le=240)


class AppointmentSlotAppointment(BaseModel):
    id: str
    conversation_id: Optional[str] = None
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


class AppointmentDayResponse(BaseModel):
    date: str
    schedule: AppointmentScheduleResponse
    slots: list[AppointmentDaySlot]


class AppointmentCreateRequest(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    start_time: str = Field(min_length=4, max_length=5)
    end_time: str = Field(min_length=4, max_length=5)
    client_name: str = Field(min_length=1, max_length=255)
    client_phone: Optional[str] = Field(default=None, max_length=64)
    notes: Optional[str] = Field(default=None, max_length=2000)
    conversation_id: Optional[uuid.UUID] = None


class AppointmentUpdateRequest(BaseModel):
    date: Optional[str] = None
    start_time: Optional[str] = Field(default=None, min_length=4, max_length=5)
    end_time: Optional[str] = Field(default=None, min_length=4, max_length=5)
    client_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    client_phone: Optional[str] = Field(default=None, max_length=64)
    notes: Optional[str] = Field(default=None, max_length=2000)


class AppointmentResponse(BaseModel):
    id: str
    conversation_id: Optional[str] = None
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

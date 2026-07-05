from __future__ import annotations

from datetime import date

from app.application.appointments.appointment_service import (
    ScheduleSettings,
    _slot_ranges,
    get_schedule,
    parse_slot_to_datetimes,
    update_schedule,
)
from app.domain.entities import TenantProfile


def test_slot_ranges_one_hour_blocks():
    schedule = ScheduleSettings(open_time="08:00", close_time="11:00", slot_minutes=60)
    slots = _slot_ranges(date(2026, 6, 29), schedule)
    assert len(slots) == 3
    assert slots[0][0].hour == 8
    assert slots[-1][1].hour == 11


def test_update_schedule_validates_close_after_open():
    profile = TenantProfile(tenant_id=None)  # type: ignore[arg-type]
    profile.schedule_open_time = "08:00"
    profile.schedule_close_time = "18:00"
    profile.schedule_slot_minutes = 60
    updated = update_schedule(profile, open_time="09:00", close_time="17:00", slot_minutes=30)
    assert updated.open_time == "09:00"
    assert updated.slot_minutes == 30


def test_parse_slot_to_datetimes():
    day = date(2026, 6, 29)
    start, end = parse_slot_to_datetimes(day, "10:00", "11:00")
    assert start.hour == 10
    assert end.hour == 11


def test_get_schedule_clamps_slot_minutes():
    profile = TenantProfile(tenant_id=None)  # type: ignore[arg-type]
    profile.schedule_slot_minutes = 5
    assert get_schedule(profile).slot_minutes == 15
    profile.schedule_slot_minutes = 500
    assert get_schedule(profile).slot_minutes == 240

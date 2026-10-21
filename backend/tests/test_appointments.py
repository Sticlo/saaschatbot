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


def test_lunch_break_has_no_slots_and_afternoon_restarts_on_time():
    schedule = ScheduleSettings(
        open_time="08:00", close_time="18:00", slot_minutes=45, break_start="13:00", break_end="14:00"
    )
    slots = [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in _slot_ranges(date(2026, 6, 29), schedule)]
    assert not any(a < "14:00" and b > "13:00" for a, b in slots)
    assert ("14:00", "14:45") in slots
    assert slots[0] == ("08:00", "08:45")


def test_owner_sets_and_clears_lunch_break():
    import pytest

    profile = TenantProfile(tenant_id=None)  # type: ignore[arg-type]
    saved = update_schedule(profile, open_time="08:00", close_time="18:00", slot_minutes=60,
                            break_start="13:00", break_end="14:00")
    assert (saved.break_start, saved.break_end) == ("13:00", "14:00")
    assert len(_slot_ranges(date(2026, 6, 29), saved)) == 9
    with pytest.raises(ValueError, match="dentro del horario"):
        update_schedule(profile, open_time="08:00", close_time="12:00", slot_minutes=60,
                        break_start="13:00", break_end="14:00")
    with pytest.raises(ValueError, match="empieza y a qué hora termina"):
        update_schedule(profile, open_time="08:00", close_time="18:00", slot_minutes=60,
                        break_start="13:00", break_end="")
    cleared = update_schedule(profile, open_time="08:00", close_time="18:00", slot_minutes=60,
                              break_start="", break_end="")
    assert cleared.break_start is None


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

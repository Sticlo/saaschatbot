"""Agenda por empleado: cada quien con su horario, preferido o cualquiera, reparto parejo."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.application.ai.ai_appointment_service import (
    BookSlotRequest,
    append_appointment_instructions,
    parse_book_slot,
)
from app.application.ai.ai_shortcut_service import AiGeneratedReply
from app.application.appointments.staff_service import find_staff_by_name
from tests.conftest import requires_db

ALL_WEEK = [0, 1, 2, 3, 4, 5, 6]


def _next_weekday(weekday: int) -> date:
    from app.application.appointments.appointment_service import BOGOTA

    today = datetime.now(BOGOTA).date()
    return today + timedelta(days=((weekday - today.weekday()) % 7) or 7)


def _night_before(day: date) -> datetime:
    from app.application.appointments.appointment_service import BOGOTA

    return datetime.combine(day - timedelta(days=1), datetime.min.time(), tzinfo=BOGOTA).replace(hour=20)


def _tenant(db):
    from app.domain.entities import Tenant, TenantProfile

    tenant = Tenant(business_name="Barbería Test", slug=f"barber-{uuid.uuid4().hex[:8]}")
    db.add(tenant)
    db.flush()
    db.add(
        TenantProfile(
            tenant_id=tenant.id, schedule_open_time="08:00", schedule_close_time="18:00", schedule_slot_minutes=60
        )
    )
    db.flush()
    return tenant


def _staff(db, tenant, name, **kwargs):
    from app.application.appointments.staff_service import create_staff

    kwargs.setdefault("work_days", ALL_WEEK)
    return create_staff(db, tenant_id=tenant.id, name=name, **kwargs)


def _book(db, tenant, day, start, end, *, staff=None, client="Cliente"):
    from app.application.appointments.appointment_service import create_appointment, parse_slot_to_datetimes

    starts, ends = parse_slot_to_datetimes(day, start, end)
    return create_appointment(
        db,
        tenant_id=tenant.id,
        starts_at=starts,
        ends_at=ends,
        client_name=client,
        staff_id=staff.id if staff else None,
    )


# --- Sin base de datos --------------------------------------------------------------------


def test_names_match_without_accents_and_never_guess_when_ambiguous():
    team = [
        SimpleNamespace(name="Mateo López"),
        SimpleNamespace(name="José Luis"),
        SimpleNamespace(name="José Pérez"),
    ]
    assert find_staff_by_name(team, "mateo").name == "Mateo López"
    assert find_staff_by_name(team, "MATÉO LOPEZ").name == "Mateo López"
    assert find_staff_by_name(team, "jose perez").name == "José Pérez"
    assert find_staff_by_name(team, "José") is None
    assert find_staff_by_name(team, "Andrés") is None


def test_book_slot_carries_the_requested_staff():
    keys = frozenset({("2026-10-08", "10:00", "11:00")})
    asked = parse_book_slot({"date": "2026-10-08", "start": "10:00", "staff": "Mateo"}, valid_keys=keys)
    anyone = parse_book_slot({"date": "2026-10-08", "start": "10:00", "staff": None}, valid_keys=keys)
    assert asked == BookSlotRequest(date="2026-10-08", start="10:00", end="11:00", staff="Mateo")
    assert anyone.staff == ""


def test_prompt_lists_who_is_free_only_when_not_everyone():
    mateo, luis = {"id": "1", "name": "Mateo"}, {"id": "2", "name": "Luis"}
    slots = [
        {"date": "2026-10-08", "start": "09:00", "end": "10:00", "label": "Mañana 09:00–10:00", "staff": [mateo, luis]},
        {"date": "2026-10-08", "start": "10:00", "end": "11:00", "label": "Mañana 10:00–11:00", "staff": [luis]},
    ]
    out = append_appointment_instructions("Base", slots, staff_label="barbero")
    assert "- Mañana (2026-10-08): 09:00, 10:00 (libres: Luis)" in out
    assert "Equipo (barberos): Mateo, Luis" in out
    assert "¿Tienes barbero de preferencia" in out
    assert '"staff"' in out


def test_prompt_without_team_stays_as_before():
    slots = [{"date": "2026-10-08", "start": "09:00", "end": "10:00", "label": "Mañana 09:00–10:00"}]
    out = append_appointment_instructions("Base", slots)
    assert "Equipo" not in out
    assert "preferencia" not in out


# --- Motor --------------------------------------------------------------------------------


@requires_db
def test_free_slots_follow_each_persons_days_and_hours():
    from app.application.appointments.appointment_service import collect_upcoming_free_slots
    from app.infrastructure.persistence.database import SessionLocal

    monday = _next_weekday(0)
    with SessionLocal() as db:
        tenant = _tenant(db)
        _staff(db, tenant, "Mateo", work_days=[0, 1, 2, 3, 4, 5])
        _staff(db, tenant, "Luis", work_days=[1, 2, 3, 4, 5], start_time="14:00")
        slots = collect_upcoming_free_slots(
            db, tenant_id=tenant.id, days=3, limit=100, now=_night_before(monday)
        )
        db.rollback()

    def who(day, start):
        return [s["name"] for s in next(x for x in slots if x["date"] == day.isoformat() and x["start"] == start)["staff"]]

    tuesday = monday + timedelta(days=1)
    assert who(monday, "15:00") == ["Mateo"]
    assert who(tuesday, "10:00") == ["Mateo"]
    assert sorted(who(tuesday, "15:00")) == ["Luis", "Mateo"]


@requires_db
def test_anyone_goes_to_the_least_busy_and_ties_go_by_order():
    from app.infrastructure.persistence.database import SessionLocal

    day = _next_weekday(2)
    with SessionLocal() as db:
        tenant = _tenant(db)
        mateo = _staff(db, tenant, "Mateo")
        luis = _staff(db, tenant, "Luis")
        first = _book(db, tenant, day, "09:00", "10:00")
        second = _book(db, tenant, day, "11:00", "12:00")
        third = _book(db, tenant, day, "13:00", "14:00")
        assigned = [first.staff_id, second.staff_id, third.staff_id]
        db.rollback()

    assert assigned == [mateo.id, luis.id, mateo.id]


@requires_db
def test_each_person_has_their_own_agenda():
    from app.infrastructure.persistence.database import SessionLocal

    day = _next_weekday(3)
    with SessionLocal() as db:
        tenant = _tenant(db)
        mateo = _staff(db, tenant, "Mateo")
        luis = _staff(db, tenant, "Luis")
        _book(db, tenant, day, "10:00", "11:00", staff=mateo)
        same_hour_other = _book(db, tenant, day, "10:00", "11:00", staff=luis)
        with pytest.raises(ValueError, match="Mateo ya tiene una cita"):
            _book(db, tenant, day, "10:00", "11:00", staff=mateo)
        with pytest.raises(ValueError, match="No hay nadie libre"):
            _book(db, tenant, day, "10:00", "11:00")
        db.rollback()

    assert same_hour_other.staff_id == luis.id


@requires_db
def test_first_person_on_the_team_inherits_existing_appointments():
    from app.infrastructure.persistence.database import SessionLocal

    day = _next_weekday(4)
    with SessionLocal() as db:
        tenant = _tenant(db)
        legacy = _book(db, tenant, day, "09:00", "10:00")
        assert legacy.staff_id is None
        mateo = _staff(db, tenant, "Mateo")
        _staff(db, tenant, "Luis")
        db.refresh(legacy)
        owner = legacy.staff_id
        db.rollback()

    assert owner == mateo.id


@requires_db
def test_cannot_remove_someone_with_upcoming_appointments():
    from app.application.appointments.staff_service import StaffError, delete_staff
    from app.infrastructure.persistence.database import SessionLocal

    day = _next_weekday(1)
    with SessionLocal() as db:
        tenant = _tenant(db)
        mateo = _staff(db, tenant, "Mateo")
        _book(db, tenant, day, "09:00", "10:00", staff=mateo)
        with pytest.raises(StaffError, match="1 cita próxima"):
            delete_staff(db, tenant_id=tenant.id, staff_id=mateo.id)
        db.rollback()


@requires_db
def test_staff_phones_are_team_numbers_not_customers():
    from app.application.conversations.interest_alert_service import is_alert_phone
    from app.domain.entities import Tenant
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _tenant(db)
        _staff(db, tenant, "Mateo", phone="+57 300 111 2233")
        db.flush()
        loaded = db.get(Tenant, tenant.id)
        db.refresh(loaded)
        assert is_alert_phone(loaded, "573001112233") is True
        assert is_alert_phone(loaded, "573009998877") is False
        db.rollback()


# --- La IA agendando con equipo -----------------------------------------------------------


def _seed_team(db, *, body: str):
    from tests.test_sales_stage_and_booking import _seed

    tenant, conv, msg = _seed(db, body=body, profile_kwargs={"ai_booking_enabled": True, "staff_label": "barbero"})
    mateo = _staff(db, tenant, "Mateo", phone="+573001112233")
    luis = _staff(db, tenant, "Luis", phone="+573004445566")
    db.commit()
    return tenant, conv, msg, mateo, luis


def _tomorrow() -> date:
    from app.application.appointments.appointment_service import BOGOTA

    return datetime.now(BOGOTA).date() + timedelta(days=1)


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_books_with_whoever_is_free_tells_the_client_and_notifies_them(
    _sleep, mock_generate, mock_send, mock_wa
):
    from app.domain.entities import Appointment
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    day = _tomorrow()
    mock_generate.return_value = AiGeneratedReply(
        message="¡Listo! Te agendé mañana a las 10:00 ✅",
        book_slot=BookSlotRequest(date=day.isoformat(), start="10:00", end="11:00"),
        stage="interesado",
    )
    with SessionLocal() as db:
        tenant, conv, msg, mateo, luis = _seed_team(db, body="Quiero un corte mañana a las 10, con cualquiera")
        _book(db, tenant, day, "09:00", "10:00", staff=mateo)
        db.commit()
        assert _run(db, tenant, conv, msg) is True
        booking = mock_generate.call_args.kwargs["booking"]
        row = db.query(Appointment).filter(Appointment.conversation_id == conv.id).one()
        assigned = row.staff_id

    assert booking.staff_label == "barbero"
    assert assigned == luis.id
    assert mock_send.call_args.kwargs["reply"].message.endswith("Te atiende Luis.")
    notified = [c.args[1] for c in mock_wa.call_args_list]
    assert notified == ["573004445566"]
    assert "Nueva cita para ti" in mock_wa.call_args.args[2]


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_never_swaps_the_requested_person_for_someone_else(_sleep, mock_generate, mock_send, _wa):
    from app.domain.entities import Appointment
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    day = _tomorrow()
    mock_generate.return_value = AiGeneratedReply(
        message="¡Listo! Te agendé con Mateo ✅",
        book_slot=BookSlotRequest(date=day.isoformat(), start="10:00", end="11:00", staff="mateo"),
    )
    with SessionLocal() as db:
        tenant, conv, msg, mateo, _luis = _seed_team(db, body="Con Mateo mañana a las 10")
        _book(db, tenant, day, "10:00", "11:00", staff=mateo)
        db.commit()
        assert _run(db, tenant, conv, msg) is True
        mine = db.query(Appointment).filter(Appointment.conversation_id == conv.id).count()

    assert mine == 0
    text = mock_send.call_args.kwargs["reply"].message
    assert "se acaba de ocupar" in text
    assert "10:00–11:00" not in text


def _spanish(day: date) -> str:
    weekdays = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    months = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
              "septiembre", "octubre", "noviembre", "diciembre"]
    return f"{weekdays[day.weekday()]} {day.day} de {months[day.month - 1]}"


def test_confirmation_text_tells_what_was_booked():
    from app.application.ai.ai_appointment_service import claims_booking, infer_claimed_booking

    juan, luis = {"id": "1", "name": "Juan"}, {"id": "2", "name": "Luis"}
    slots = [
        {"date": "2026-10-21", "start": "10:00", "end": "11:00", "staff": [juan, luis]},
        {"date": "2026-10-21", "start": "15:00", "end": "16:00", "staff": [luis]},
    ]
    today = date(2026, 10, 7)
    said = "Listo, Sebastián. Te dejo agendado el miércoles 21 de octubre a las 10:00 a. m. con Juan para corte y barba."
    assert claims_booking(said)
    assert infer_claimed_booking(said, slots, today=today) == BookSlotRequest(
        date="2026-10-21", start="10:00", end="11:00", staff="Juan"
    )
    assert infer_claimed_booking("Te agendé el 21 de octubre a las 3 de la tarde", slots, today=today).start == "15:00"
    assert infer_claimed_booking("Te agendé el 21 de octubre a las 3 pm con Juan", slots, today=today) is None
    assert infer_claimed_booking("Tengo libre el 21 de octubre a las 10:00, ¿te sirve?", slots, today=today) is None
    assert not claims_booking("¿Te agendo el miércoles a las 10?")


def test_stalling_on_the_agenda_is_detected_but_closing_a_sale_is_not():
    from app.application.ai.ai_appointment_service import stalls_on_agenda

    slots = [{"date": "2026-10-21", "start": "14:00", "end": "15:00", "staff": [{"id": "1", "name": "Luis"}]}]
    assert stalls_on_agenda(
        "Entiendo, Sebastián. Déjame revisarte si a las 2:00 p.m. del 21 de octubre hay otro profesional libre "
        "para el manicure y pedicure de tu esposa, y ya te confirmo en un momentico.",
        "No, no si o si déjalo para las 2 pero con otro chamo",
        slots,
    )
    assert stalls_on_agenda("¡De una! Te confirmo en un momentico la cita del miércoles.", "déjalo así", slots)
    assert stalls_on_agenda("Ya te confirmo si Luis puede.", "y con luis?", slots)
    assert not stalls_on_agenda("¡De una! Dame un momentico y te confirmo 🙌", "Listo, ¿cómo pago?", slots)
    assert not stalls_on_agenda("Si me dices el día, te confirmo disponibilidad.", "quiero una cita", slots)


def test_promise_to_write_later_is_a_stall_when_the_chat_is_about_the_agenda():
    from app.application.ai.ai_appointment_service import (
        pending_promise_note,
        promises_follow_up,
        stalls_on_agenda,
    )

    later = "Perfecto, Sebastián. En cuanto tenga la confirmación te escribo por aquí."
    before = "Déjame revisarte si a las 2:00 p.m. del 21 hay otro profesional libre, y ya te confirmo en un momentico."
    assert promises_follow_up(later)
    assert stalls_on_agenda(later, f"Ok {before}", [])
    assert not stalls_on_agenda(later, "Ok", [])
    assert "Él está esperando esa respuesta" in pending_promise_note(before)
    assert pending_promise_note("Dame un momentico y te confirmo el pago 🙌") == ""
    assert pending_promise_note("¿Te lo dejo agendado así?") == ""


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ok_after_a_pending_promise_gets_the_answer_not_another_promise(_sleep, mock_generate, mock_send, _wa):
    from datetime import timezone

    from app.domain.entities import Message
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    mock_generate.side_effect = [
        AiGeneratedReply(message="Perfecto. En cuanto tenga la confirmación te escribo por aquí."),
        AiGeneratedReply(message="Listo: mañana a las 10:00 está libre Luis. ¿Te la dejo con él?"),
    ]
    with SessionLocal() as db:
        tenant, conv, msg, _mateo, _luis = _seed_team(db, body="Ok")
        now = datetime.now(timezone.utc)
        msg.created_at = now
        db.add(Message(
            tenant_id=tenant.id, conversation_id=conv.id, direction="out", source="bot", status="sent",
            body="Déjame revisar si mañana a las 10:00 hay otro profesional libre y ya te confirmo en un momentico.",
            created_at=now - timedelta(minutes=1),
        ))
        db.commit()
        assert _run(db, tenant, conv, msg) is True

    first_note = mock_generate.call_args_list[0].kwargs["correction"]
    assert "Él está esperando esa respuesta" in first_note
    assert mock_send.call_args.kwargs["reply"].message.startswith("Listo: mañana a las 10:00")
    assert conv.ai_active is True


def test_colombian_acknowledgments_are_just_a_yes():
    from app.application.ai.ai_appointment_service import is_short_ack

    for text in ("Epa", "Breve", "De una", "Hágale pues", "Sisas", "Listo parce", "Melo", "ok gracias", "Dale"):
        assert is_short_ack(text), text
    for text in ("Breve déjalo con el chamo luis", "y para mi hijo también", "a las 3", "epa y el precio?"):
        assert not is_short_ack(text), text


def test_taken_slot_alternatives_start_with_the_same_day_and_closest_hour():
    from app.application.ai.ai_appointment_service import nearest_slots

    slots = [
        {"date": "2026-10-08", "start": "08:00"},
        {"date": "2026-10-21", "start": "08:00"},
        {"date": "2026-10-21", "start": "13:00"},
        {"date": "2026-10-21", "start": "15:00"},
        {"date": "2026-10-22", "start": "14:00"},
    ]
    picked = nearest_slots(slots, day="2026-10-21", start="14:00")
    assert [(s["date"], s["start"]) for s in picked] == [
        ("2026-10-21", "13:00"), ("2026-10-21", "15:00"), ("2026-10-21", "08:00")
    ]


def _far_workday() -> date:
    far = _tomorrow() + timedelta(days=20)
    while far.weekday() == 6:
        far += timedelta(days=1)
    return far


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_epa_after_booking_does_not_book_again_nor_say_the_slot_was_taken(_sleep, mock_generate, mock_send, _wa):
    from app.application.appointments.appointment_service import BOGOTA, create_appointment
    from app.domain.entities import Appointment
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    far = _far_workday()
    mock_generate.return_value = AiGeneratedReply(
        message="¡Listo! Quedó la cita con Luis 🙌",
        book_slot=BookSlotRequest(date=far.isoformat(), start="10:00", end="11:00", staff="Luis"),
    )
    with SessionLocal() as db:
        tenant, conv, msg, _mateo, luis = _seed_team(db, body="Epa")
        starts = datetime.combine(far, datetime.min.time(), tzinfo=BOGOTA).replace(hour=10)
        create_appointment(
            db, tenant_id=tenant.id, starts_at=starts, ends_at=starts + timedelta(hours=1),
            client_name="Laura", conversation_id=conv.id, staff_id=luis.id,
        )
        db.commit()
        assert _run(db, tenant, conv, msg) is True
        count = db.query(Appointment).filter(Appointment.conversation_id == conv.id).count()

    assert count == 1
    assert mock_send.call_args.kwargs["reply"].message == "¡Listo! Quedó la cita con Luis 🙌"


def test_another_person_is_told_apart_from_the_same_client():
    from app.application.ai.ai_appointment_service import mentions_another_person

    for text in ("y para mi esposa a la misma hora", "mi hijo también", "quiero otra cita", "agéndanos a los dos"):
        assert mentions_another_person(text), text
    for text in ("Quiero ver cómo corta Juan", "Si, confirmo.", "para mí está bien", "a las 11 con Juan"):
        assert not mentions_another_person(text), text


def _book_again_at_ten(mock_generate, *, client_text: str, reply_text: str) -> int:
    from app.application.appointments.appointment_service import BOGOTA, create_appointment
    from app.domain.entities import Appointment
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    far = _far_workday()
    mock_generate.return_value = AiGeneratedReply(
        message=reply_text, book_slot=BookSlotRequest(date=far.isoformat(), start="10:00", end="11:00")
    )
    with SessionLocal() as db:
        tenant, conv, msg, mateo, _luis = _seed_team(db, body=client_text)
        starts = datetime.combine(far, datetime.min.time(), tzinfo=BOGOTA).replace(hour=10)
        create_appointment(
            db, tenant_id=tenant.id, starts_at=starts, ends_at=starts + timedelta(hours=1),
            client_name="Sebastián", conversation_id=conv.id, staff_id=mateo.id,
        )
        db.commit()
        assert _run(db, tenant, conv, msg) is True
        return db.query(Appointment).filter(Appointment.conversation_id == conv.id).count()


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_restating_the_appointment_does_not_book_a_second_one_with_someone_else(
    _sleep, mock_generate, mock_send, _wa
):
    """«Quiero ver cómo corta Mateo» → «tu cita ya quedó con Mateo» + book_slot sin persona: antes se
    creaba otra cita a esa hora con el primero libre."""
    count = _book_again_at_ten(
        mock_generate,
        client_text="Quiero ver cómo corta Mateo",
        reply_text="Sebastián, tu cita ya quedó agendada a las 10:00 con Mateo. ¿Necesitas algo más?",
    )

    assert count == 1
    assert mock_send.call_args.kwargs["reply"].message.endswith("con Mateo. ¿Necesitas algo más?")


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_a_second_appointment_for_the_wife_at_the_same_hour_is_booked(_sleep, mock_generate, mock_send, _wa):
    count = _book_again_at_ten(
        mock_generate,
        client_text="y para mi esposa a la misma hora",
        reply_text="¡Listo! Tu esposa también quedó a las 10:00.",
    )

    assert count == 2


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_a_truly_taken_slot_offers_hours_of_that_same_day(_sleep, mock_generate, mock_send, _wa):
    from app.application.appointments.appointment_service import BOGOTA, create_appointment
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    far = _far_workday()
    mock_generate.return_value = AiGeneratedReply(
        message="¡Listo! Te agendé con Luis.",
        book_slot=BookSlotRequest(date=far.isoformat(), start="10:00", end="11:00", staff="Luis"),
    )
    with SessionLocal() as db:
        tenant, conv, msg, _mateo, luis = _seed_team(db, body="déjalo con Luis a las 10")
        starts = datetime.combine(far, datetime.min.time(), tzinfo=BOGOTA).replace(hour=10)
        create_appointment(
            db, tenant_id=tenant.id, starts_at=starts, ends_at=starts + timedelta(hours=1),
            client_name="Otro cliente", staff_id=luis.id,
        )
        db.commit()
        assert _run(db, tenant, conv, msg) is True

    text = mock_send.call_args.kwargs["reply"].message
    assert text.startswith("Uy, ese horario se acaba de ocupar")
    assert "Mañana" not in text
    assert "09:00" in text and "11:00" in text


def test_requested_hour_gets_a_verified_note_of_who_is_free():
    from app.application.ai.ai_appointment_service import requested_slot_note

    juan, luis = {"id": "1", "name": "Juan"}, {"id": "2", "name": "Luis"}
    slots = [
        {"date": "2026-10-21", "start": "13:00", "end": "14:00", "staff": [juan, luis]},
        {"date": "2026-10-21", "start": "14:00", "end": "15:00", "staff": [luis]},
    ]
    note = requested_slot_note("déjalo para las 2 pero con otro chamo", slots, "2026-10-21")
    assert "el 2026-10-21 a las 14:00 están libres Luis" in note
    assert "no hay nadie libre" in requested_slot_note("a las 5 pm", slots, "2026-10-21")
    assert requested_slot_note("a las 2", slots, None) == ""
    assert requested_slot_note("perfecto", slots, "2026-10-21") == ""


def test_hour_of_the_clients_own_appointment_is_not_reported_as_busy():
    """Agendó el 14 a las 11 con Juan y el cliente dice «Sí, confirmo»: Juan ya no sale libre a esa
    hora porque es su propia cita, no porque esté ocupado con otro."""
    from app.application.ai.ai_appointment_service import requested_slot_note

    sebastian, luis = {"id": "2", "name": "Sebastian"}, {"id": "3", "name": "Luis"}
    slots = [{"date": "2026-10-14", "start": "11:00", "end": "12:00", "staff": [sebastian, luis]}]
    booked = (("2026-10-14", "11:00", "Juan"),)

    note = requested_slot_note("Quiero con Juan, el día 14 de octubre a las 11 am", slots, "2026-10-14", booked)

    assert "YA tiene su cita agendada con Juan" in note
    assert "No digas que no hay disponibilidad" in note
    assert "para otra persona), están libres Sebastian, Luis" in note
    assert "Puedes agendar a esa hora" not in note


def test_reply_denying_the_clients_own_booking_is_replaced_by_its_confirmation():
    from app.application.ai.ai_appointment_service import denies_own_booking, own_booking_message

    booked = (("2026-10-14", "11:00", "Juan"),)
    wrong = (
        "Sebastián, revisé la agenda y el miércoles 14 de octubre a las 11:00 a.m. Juan no tiene "
        "disponibilidad. A esa hora están libres Sebastian y Luis."
    )

    entry = denies_own_booking(wrong, booked)

    assert entry == booked[0]
    assert own_booking_message(entry) == (
        "¡Listo! Tu cita ya quedó agendada para el miércoles 14 de octubre a las 11:00 con Juan ✅"
    )
    assert denies_own_booking("¡Listo! Tu cita quedó con Juan a las 11:00 ✅", booked) is None
    assert denies_own_booking("Luis no tiene disponibilidad a las 3 pm", booked) is None
    assert denies_own_booking("Juan no tiene disponibilidad a las 11", ()) is None


def test_confirming_right_after_the_ai_booked_does_not_hand_the_chat_off():
    from app.application.ai.ai_appointment_service import BookingContext
    from app.application.ai.ai_service import _resolve_stage

    message = SimpleNamespace(body="Si, confirmo.", transcript=None)
    reply = AiGeneratedReply(message="¡Listo! Tu cita quedó agendada ✅")

    with_appointment = BookingContext(free_slots=[], booked=(("2026-10-14", "11:00", "Juan"),))
    assert _resolve_stage(reply, message=message, shortcuts=[], booking=with_appointment) != "cierre"
    assert _resolve_stage(reply, message=message, shortcuts=[], booking=BookingContext(free_slots=[])) == "cierre"


def test_prompt_says_explicitly_who_is_free_at_each_hour():
    juan, luis = {"id": "1", "name": "Juan"}, {"id": "2", "name": "Luis"}
    slots = [
        {"date": "2026-10-21", "start": "13:00", "end": "14:00", "label": "Mié 13:00–14:00", "staff": [juan, luis]},
        {"date": "2026-10-21", "start": "14:00", "end": "15:00", "label": "Mié 14:00–15:00", "staff": [luis]},
    ]
    out = append_appointment_instructions("Base", slots, existing_appointment="Mié a las 14:00 con Juan")
    assert "- Mié (2026-10-21): 13:00, 14:00 (libres: Luis)" in out
    assert "agéndala como una cita nueva" in out
    assert "nunca digas \"déjame revisar\"" in out


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_that_stalls_on_the_agenda_is_asked_again(_sleep, mock_generate, mock_send, _wa):
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    mock_generate.side_effect = [
        AiGeneratedReply(message="Déjame revisar si Luis está libre a las 10 y ya te confirmo."),
        AiGeneratedReply(message="Sí, mañana a las 10:00 Luis está libre. ¿Te lo dejo agendado?"),
    ]
    with SessionLocal() as db:
        tenant, conv, msg, _mateo, _luis = _seed_team(db, body="¿Mañana a las 10 con Luis?")
        assert _run(db, tenant, conv, msg) is True

    assert mock_generate.call_count == 2
    assert "ibas a responder «Déjame revisar" in mock_generate.call_args.kwargs["correction"]
    assert mock_send.call_args.kwargs["reply"].message.startswith("Sí, mañana a las 10:00 Luis")


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_that_keeps_stalling_is_replaced_by_the_free_hours(_sleep, mock_generate, mock_send, _wa):
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    mock_generate.return_value = AiGeneratedReply(message="Déjame revisar la agenda y ya te confirmo.")
    with SessionLocal() as db:
        tenant, conv, msg, _mateo, _luis = _seed_team(db, body="¿Qué horario tienes mañana?")
        assert _run(db, tenant, conv, msg) is True

    text = mock_send.call_args.kwargs["reply"].message
    assert text.startswith("Te cuento lo que tengo libre")
    assert "¿Cuál te sirve?" in text
    assert conv.ai_active is True


@requires_db
def test_ai_sees_the_day_of_the_clients_appointment_even_if_nobody_repeats_it():
    from app.application.ai.ai_service import _booking_context
    from app.application.appointments.appointment_service import BOGOTA, create_appointment
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, msg, mateo, _luis = _seed_team(db, body="mi esposa también quiere ese día a la misma hora")
        far = _tomorrow() + timedelta(days=20)
        while far.weekday() == 6:
            far += timedelta(days=1)
        starts = datetime.combine(far, datetime.min.time(), tzinfo=BOGOTA).replace(hour=10)
        create_appointment(
            db, tenant_id=tenant.id, starts_at=starts, ends_at=starts + timedelta(hours=1),
            client_name="Laura", conversation_id=conv.id, staff_id=mateo.id,
        )
        db.commit()
        booking = _booking_context(db, tenant=tenant, profile=tenant.profile, conversation=conv, history=[msg])

    assert far.isoformat() in booking.extra_days
    at_ten = [s for s in booking.free_slots if s["date"] == far.isoformat() and s["start"] == "10:00"]
    assert [m["name"] for m in at_ten[0]["staff"]] == ["Luis"]


def test_offers_and_questions_are_not_booking_claims():
    from app.application.ai.ai_appointment_service import claims_booking

    assert not claims_booking(
        "¡Claro que sí! El corte de niño tiene un valor de $700.\n\n"
        "¿Quieres que te agende una cita? Si me dices el día y la hora que te sirvan, te confirmo disponibilidad."
    )
    assert not claims_booking("Si me dices la hora, te dejo agendado de una.")
    assert not claims_booking("Apenas me confirmes, tu cita queda lista.")
    assert claims_booking("¡Listo! Te agendé para mañana a las 10:00.")
    assert claims_booking("Perfecto, ya te agende con Juan.")
    assert claims_booking("Tu cita quedó para el jueves a las 3 p. m. ¿Algo más?")


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_that_says_booked_without_book_slot_still_books(_sleep, mock_generate, mock_send, _wa):
    from app.application.appointments.appointment_service import BOGOTA
    from app.domain.entities import Appointment
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    day = _tomorrow() + timedelta(days=20)
    mock_generate.return_value = AiGeneratedReply(
        message=f"Listo, Sebastián. Te dejo agendado el {_spanish(day)} a las 10:00 a. m. con Luis para corte y barba.",
        stage="interesado",
    )
    with SessionLocal() as db:
        tenant, conv, msg, _mateo, luis = _seed_team(db, body=f"déjalo entonces para el {day.day} de {_spanish(day).split(' de ')[-1]}")
        assert _run(db, tenant, conv, msg) is True
        row = db.query(Appointment).filter(Appointment.conversation_id == conv.id).one()
        booked = (row.staff_id, row.starts_at.astimezone(BOGOTA).strftime("%Y-%m-%d %H:%M"))

    assert booked == (luis.id, f"{day.isoformat()} 10:00")
    assert "agendado" in mock_send.call_args.kwargs["reply"].message


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_never_tells_a_client_they_are_booked_when_they_are_not(_sleep, mock_generate, mock_send, _alert):
    from app.domain.entities import Appointment
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run, _seed

    mock_generate.return_value = AiGeneratedReply(message="Listo, te dejo agendado mañana a las 10:00 con Juan.")
    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="agéndame mañana a las 10")  # agenda de la IA apagada
        assert _run(db, tenant, conv, msg) is True
        count = db.query(Appointment).filter(Appointment.conversation_id == conv.id).count()

    assert count == 0
    text = mock_send.call_args.kwargs["reply"].message
    assert "agendado" not in text
    assert "te confirmen la hora" in text
    assert conv.mode == "manual"


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_claim_without_a_valid_slot_asks_for_one_and_keeps_the_ai(_sleep, mock_generate, mock_send, _wa):
    from app.domain.entities import Appointment
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    mock_generate.return_value = AiGeneratedReply(message="¡Listo! Te dejo agendado para el corte de tu hijo.")
    with SessionLocal() as db:
        tenant, conv, msg, _mateo, _luis = _seed_team(db, body="Me gustaría un corte de niño para mi hijo")
        assert _run(db, tenant, conv, msg) is True
        count = db.query(Appointment).filter(Appointment.conversation_id == conv.id).count()

    assert count == 0
    text = mock_send.call_args.kwargs["reply"].message
    assert text.startswith("Para dejarte la cita agendada")
    assert "¿Cuál te sirve?" in text
    assert conv.mode != "manual" and conv.ai_active is True


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_offering_to_book_is_sent_as_is(_sleep, mock_generate, mock_send, _wa):
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_sales_stage_and_booking import _run

    offer = "El corte de niño vale $700. ¿Quieres que te agende una cita? Si me dices el día y la hora, te confirmo."
    mock_generate.return_value = AiGeneratedReply(message=offer)
    with SessionLocal() as db:
        tenant, conv, msg, _mateo, _luis = _seed_team(db, body="Me gustaría un corte de niño para mi hijo")
        assert _run(db, tenant, conv, msg) is True

    assert mock_send.call_args.kwargs["reply"].message == offer
    assert conv.ai_active is True


# --- API ----------------------------------------------------------------------------------


def _owner_headers(client: TestClient) -> dict:
    tag = uuid.uuid4().hex[:8]
    res = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Barbería {tag}",
            "owner_name": "Dueño",
            "email": f"barber-{tag}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
def test_team_api_end_to_end(_wa, client: TestClient):
    headers = _owner_headers(client)
    base = "/api/v1/appointments"
    day = _tomorrow().isoformat()

    saved = client.put(
        f"{base}/schedule",
        headers=headers,
        json={"open_time": "08:00", "close_time": "18:00", "slot_minutes": 60, "staff_label": "Barbero"},
    )
    assert saved.json()["staff_label"] == "barbero"

    mateo = client.post(f"{base}/staff", headers=headers, json={"name": "Mateo", "work_days": ALL_WEEK})
    assert mateo.status_code == 201, mateo.text
    luis = client.post(
        f"{base}/staff",
        headers=headers,
        json={"name": "Luis", "phone": "+57 300 444 5566", "work_days": ALL_WEEK, "start_time": "12:00"},
    )
    assert luis.json()["start_time"] == "12:00"
    duplicate = client.post(f"{base}/staff", headers=headers, json={"name": "mateo", "work_days": [0]})
    assert duplicate.status_code == 400
    assert [s["name"] for s in client.get(f"{base}/staff", headers=headers).json()] == ["Mateo", "Luis"]

    auto = client.post(
        base,
        headers=headers,
        json={"date": day, "start_time": "09:00", "end_time": "10:00", "client_name": "Ana"},
    )
    assert auto.status_code == 201, auto.text
    assert auto.json()["staff_name"] == "Mateo"

    view = client.get(f"{base}/day", headers=headers, params={"day": day, "staff_id": luis.json()["id"]}).json()
    assert view["staff_id"] == luis.json()["id"]
    assert view["slots"][0]["start"] == "12:00"
    assert len(view["staff"]) == 2

    moved = client.patch(f"{base}/{auto.json()['id']}", headers=headers, json={"staff_id": luis.json()["id"]})
    assert moved.json()["staff_name"] == "Luis"
    blocked = client.delete(f"{base}/staff/{luis.json()['id']}", headers=headers)
    assert blocked.status_code == 409
    assert "Pásalas a otra persona" in blocked.json()["detail"]
    assert client.delete(f"{base}/staff/{mateo.json()['id']}", headers=headers).status_code == 204

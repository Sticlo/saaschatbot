"""Lo que Omitel le generó al negocio: solo cuenta lo que hizo la IA, sobrevive a desvincular
WhatsApp y se le muestra al dueño en el panel, en el resumen mensual y en los cobros."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application.appointments.appointment_service import BOGOTA
from app.application.results.results_service import (
    ResultsSummary,
    compute_results,
    is_after_hours,
    previous_month_period,
    results_paragraphs,
    results_whatsapp_text,
)
from tests.conftest import make_wa_tenant, requires_db

MONTH_START = datetime(2026, 9, 1, tzinfo=BOGOTA)
MONTH_END = datetime(2026, 10, 1, tzinfo=BOGOTA)


def _reload(db, tenant):
    from app.domain.entities import Tenant

    return db.get(Tenant, tenant.id)


def _local(day: int, hour: int) -> datetime:
    return datetime(2026, 9, day, hour, 0, tzinfo=BOGOTA)


def _profile(db, tenant_id, *, ticket=None):
    from app.domain.entities import TenantProfile

    profile = TenantProfile(
        tenant_id=tenant_id,
        schedule_open_time="08:00",
        schedule_close_time="18:00",
        schedule_slot_minutes=60,
        avg_ticket_cop=ticket,
    )
    db.add(profile)
    db.flush()
    return profile


def _booked(db, tenant_id, *, booked_at: datetime, source: str, slot_day: int = 20, slot_hour: int = 10):
    from app.domain.entities import Appointment

    starts = datetime(2026, 9, slot_day, slot_hour, tzinfo=BOGOTA)
    row = Appointment(
        tenant_id=tenant_id,
        starts_at=starts,
        ends_at=starts + timedelta(hours=1),
        client_name="Cliente",
        source=source,
        created_at=booked_at,
    )
    db.add(row)
    db.flush()
    return row


def _summary(**overrides) -> ResultsSummary:
    data = dict(
        start=MONTH_START,
        end=MONTH_END,
        ai_appointments=38,
        after_hours_appointments=14,
        ai_replies=412,
        after_hours_replies=120,
        clients_attended=96,
        avg_ticket_cop=30_000,
        revenue_cop=1_140_000,
        plan_price_cop=200_000,
    )
    data.update(overrides)
    return ResultsSummary(**data)


# --- Cálculo ------------------------------------------------------------------------------


@requires_db
def test_only_appointments_booked_by_the_ai_count_and_night_bookings_are_after_hours():
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, _wa = make_wa_tenant(db, label="Results")
        _profile(db, tenant.id, ticket=30_000)
        _booked(db, tenant.id, booked_at=_local(3, 22), source="ai")
        _booked(db, tenant.id, booked_at=_local(4, 10), source="ai", slot_hour=11)
        _booked(db, tenant.id, booked_at=_local(5, 23), source="panel", slot_hour=12)
        _booked(db, tenant.id, booked_at=datetime(2026, 8, 31, 22, tzinfo=BOGOTA), source="ai", slot_hour=13)
        db.commit()

        summary = compute_results(db, _reload(db, tenant), start=MONTH_START, end=MONTH_END, plan_price_cop=20_000)

    assert summary.ai_appointments == 2, "las del panel y las de otro mes no cuentan"
    assert summary.after_hours_appointments == 1
    assert summary.revenue_cop == 60_000
    assert summary.roi_multiple == 3.0


@requires_db
def test_without_an_average_ticket_there_is_no_money_figure():
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, _wa = make_wa_tenant(db, label="Results")
        _profile(db, tenant.id)
        _booked(db, tenant.id, booked_at=_local(3, 10), source="ai")
        db.commit()
        summary = compute_results(db, _reload(db, tenant), start=MONTH_START, end=MONTH_END, plan_price_cop=200_000)

    assert summary.ai_appointments == 1
    assert summary.revenue_cop is None
    assert summary.roi_multiple is None


def test_days_nobody_works_are_after_hours():
    weekdays = {0, 1, 2, 3, 4}
    sunday_morning = datetime(2026, 9, 6, 10, tzinfo=BOGOTA)
    monday_morning = datetime(2026, 9, 7, 10, tzinfo=BOGOTA)
    hours = dict(open_time=time(8, 0), close_time=time(18, 0))

    assert is_after_hours(sunday_morning, working_days=weekdays, **hours)
    assert not is_after_hours(monday_morning, working_days=weekdays, **hours)
    assert not is_after_hours(sunday_morning, working_days=None, **hours), "sin equipo no se sabe qué días abre"
    assert is_after_hours(monday_morning.replace(hour=18), working_days=None, **hours)


@requires_db
def test_ai_replies_still_count_after_unlinking_whatsapp(wa_offline):
    from app.application.conversations.whatsapp_conversation_service import (
        purge_all_tenant_whatsapp_conversations,
    )
    from app.domain.entities import Conversation, Message
    from app.domain.entities.enums import MessageDirection, MessageSource, MessageStatus
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db, label="Results")
        _profile(db, tenant.id)
        for phone in ("+573001110001", "+573001110002"):
            conversation = Conversation(
                tenant_id=tenant.id, contact_phone=phone, whatsapp_connection_id=wa.active_connection_id
            )
            db.add(conversation)
            db.flush()
            for source in (MessageSource.BOT.value, MessageSource.BOT.value, MessageSource.AGENT.value):
                db.add(
                    Message(
                        tenant_id=tenant.id,
                        conversation_id=conversation.id,
                        direction=MessageDirection.OUT.value,
                        source=source,
                        body="hola",
                        status=MessageStatus.SENT.value,
                    )
                )
        db.commit()

        purge_all_tenant_whatsapp_conversations(db, tenant_id=tenant.id)
        db.commit()
        assert db.query(Message).filter(Message.tenant_id == tenant.id).count() == 0

        now = datetime.now(timezone.utc)
        summary = compute_results(
            db, _reload(db, tenant), start=now - timedelta(hours=1), end=now + timedelta(minutes=1)
        )

    assert summary.ai_replies == 4, "las respuestas del equipo no cuentan, las de la IA sobreviven"
    assert summary.clients_attended == 2


@requires_db
def test_ai_bookings_are_marked_as_ai_and_panel_bookings_as_panel():
    from app.application.ai.ai_appointment_service import try_create_booking
    from app.application.appointments.appointment_service import create_appointment
    from app.domain.entities import Conversation, Tenant
    from app.infrastructure.persistence.database import SessionLocal

    day = datetime.now(BOGOTA).date() + timedelta(days=3)
    with SessionLocal() as db:
        tenant, wa = make_wa_tenant(db, label="Results")
        _profile(db, tenant.id)
        conversation = Conversation(
            tenant_id=tenant.id, contact_phone="+573001112233", contact_name="Ana",
            whatsapp_connection_id=wa.active_connection_id,
        )
        db.add(conversation)
        db.flush()
        ai_row = try_create_booking(
            db,
            tenant=db.get(Tenant, tenant.id),
            conversation=conversation,
            slot={"date": day.isoformat(), "start": "10:00", "end": "11:00"},
            client_name="Ana",
        )
        start = datetime.combine(day, time(12, 0), tzinfo=BOGOTA)
        panel_row = create_appointment(
            db, tenant_id=tenant.id, starts_at=start, ends_at=start + timedelta(hours=1), client_name="Luis"
        )
        db.commit()

        assert ai_row is not None and ai_row.source == "ai"
        assert panel_row.source == "panel"


# --- Textos -------------------------------------------------------------------------------


def test_the_summary_reads_like_money_and_does_not_mention_what_did_not_happen():
    text = " ".join(results_paragraphs(_summary(), period="En septiembre"))
    assert "En septiembre la IA de Omitel agendó 38 citas" in text
    assert "14 de ellas fuera de tu horario" in text
    assert "$1.140.000" in text
    assert "5,7 veces" in text
    assert "412 mensajes a 96 clientes" in text

    quiet = results_paragraphs(
        _summary(ai_appointments=0, after_hours_appointments=0, revenue_cop=None), period="En septiembre"
    )
    assert quiet[0].startswith("En septiembre la IA respondió 412 mensajes")
    assert not any("cita" in line or "veces" in line for line in quiet)


def test_whatsapp_summary_for_the_owner():
    text = results_whatsapp_text("Blade Studio", _summary(), period="En septiembre", panel_url="https://x/panel")
    assert text.startswith("📊 *Blade Studio* — en septiembre con Omitel:")
    assert "📅 38 citas agendadas por la IA (14 fuera de horario)" in text
    assert "💰 ≈ $1.140.000 en ventas" in text
    assert "🚀 5,7 veces lo que cuesta tu plan" in text
    assert text.endswith("https://x/panel")


def test_previous_month_runs_from_the_first_to_the_first_in_colombia():
    start, end = previous_month_period(datetime(2026, 10, 1, 5, 30, tzinfo=timezone.utc))
    assert (start.date(), end.date()) == (date(2026, 9, 1), date(2026, 10, 1))
    start, end = previous_month_period(datetime(2026, 1, 15, tzinfo=timezone.utc))
    assert (start.date(), end.date()) == (date(2025, 12, 1), date(2026, 1, 1))


# --- Resumen mensual ----------------------------------------------------------------------


def _owner(db, tenant_id):
    from app.domain.entities import User
    from app.domain.entities.enums import UserRole

    db.add(User(tenant_id=tenant_id, email=f"owner-{uuid.uuid4().hex[:8]}@test.com", role=UserRole.OWNER.value))


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alerts", return_value=1)
@patch("app.infrastructure.email.email_service.send_billing_email")
def test_monthly_report_goes_to_the_owner_by_email_and_whatsapp_once(mock_email, mock_alerts):
    from app.application.results.monthly_report import _claim, send_tenant_report
    from app.domain.entities import Tenant
    from app.infrastructure.persistence.database import SessionLocal

    now = datetime(2026, 10, 2, 9, tzinfo=BOGOTA)
    with SessionLocal() as db:
        tenant, _wa = make_wa_tenant(db, label="Results")
        profile = _profile(db, tenant.id, ticket=30_000)
        profile.alert_recipients = [
            {"name": "Dueño", "phone": "+573150001111", "scope": "all"},
            {"name": "Caja", "phone": "+573150002222", "scope": "sales"},
        ]
        _owner(db, tenant.id)
        _booked(db, tenant.id, booked_at=_local(10, 21), source="ai")
        db.commit()

        assert send_tenant_report(db, db.get(Tenant, tenant.id), now=now) is True
        assert _claim(db, profile.id, "2026-09") is True
        assert _claim(db, profile.id, "2026-09") is False, "un solo resumen por mes aunque haya varios workers"

    email = mock_email.call_args.kwargs
    assert email["subject"] == "Lo que Omitel hizo por Results Test en septiembre"
    assert any("$30.000" in p for p in email["paragraphs"])
    phones = mock_alerts.call_args.args[1]
    assert phones == ["+573150001111"], "solo a quienes reciben todas las alertas, nunca a clientes"
    assert "1 cita agendada por la IA (1 fuera de horario)" in mock_alerts.call_args.args[2]


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alerts")
@patch("app.infrastructure.email.email_service.send_billing_email")
def test_no_report_when_the_ai_did_nothing(mock_email, mock_alerts):
    from app.application.results.monthly_report import send_tenant_report
    from app.domain.entities import Tenant
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, _wa = make_wa_tenant(db, label="Results")
        _profile(db, tenant.id)
        _owner(db, tenant.id)
        db.commit()
        sent = send_tenant_report(db, db.get(Tenant, tenant.id), now=datetime(2026, 10, 2, tzinfo=BOGOTA))

    assert sent is False
    mock_email.assert_not_called()
    mock_alerts.assert_not_called()


def test_monthly_reports_only_go_out_in_the_first_days_of_the_month():
    from app.application.results.monthly_report import send_monthly_reports

    assert send_monthly_reports(None, datetime(2026, 10, 20, tzinfo=BOGOTA)) == 0


# --- Cobros y API -------------------------------------------------------------------------


@requires_db
def test_renewal_email_shows_what_the_ai_generated():
    from app.application.results.results_service import value_paragraph
    from app.domain.entities import Tenant
    from app.infrastructure.persistence.database import SessionLocal

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        tenant, _wa = make_wa_tenant(db, label="Results")
        _profile(db, tenant.id, ticket=100_000)
        for hours in (2, 5, 30):
            _booked(db, tenant.id, booked_at=now - timedelta(hours=hours), source="ai")
        db.commit()
        tenant = db.get(Tenant, tenant.id)
        line = value_paragraph(db, tenant, now)
        trial = value_paragraph(db, tenant, now, period="Durante tu prueba")

    assert line.startswith("En los últimos 30 días la IA agendó 3 citas por WhatsApp")
    assert "$300.000" in line
    assert "veces lo que cuesta tu plan" in line
    assert trial.startswith("Durante tu prueba la IA agendó 3 citas")


def _owner_headers(client: TestClient) -> dict:
    tag = uuid.uuid4().hex[:8]
    res = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Barbería {tag}",
            "owner_name": "Dueño",
            "email": f"results-{tag}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@requires_db
def test_results_api_and_average_ticket(client: TestClient):
    headers = _owner_headers(client)

    empty = client.get("/api/v1/results", headers=headers)
    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert body["month"]["ai_appointments"] == 0
    assert body["avg_ticket_cop"] is None
    assert body["plan_price_cop"]

    saved = client.put("/api/v1/results/settings", headers=headers, json={"avg_ticket_cop": 35000})
    assert saved.status_code == 200, saved.text
    assert saved.json()["avg_ticket_cop"] == 35000

    too_high = client.put("/api/v1/results/settings", headers=headers, json={"avg_ticket_cop": 900_000_000})
    assert too_high.status_code == 400

    cleared = client.put("/api/v1/results/settings", headers=headers, json={"avg_ticket_cop": None})
    assert cleared.json()["avg_ticket_cop"] is None

"""Interés por etapa de venta (sin traspaso brusco) y citas agendadas por la IA."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application.ai.ai_appointment_service import (
    BookingContext,
    BookSlotRequest,
    append_appointment_instructions,
    parse_book_slot,
)
from app.application.ai.ai_classifier_service import classify_inbound_message
from app.application.ai.ai_qualify_service import detect_closing_intent, detect_interest_signal
from app.application.ai.ai_shortcut_service import AiGeneratedReply, parse_ai_reply
from tests.conftest import requires_db

CATALOG = {
    "id": "cat-1",
    "label": "Catálogo",
    "type": "document",
    "file_path": "assets/x/cat.pdf",
    "file_name": "catalogo.pdf",
}


def test_reply_carries_sales_stage():
    raw = json.dumps({"message": "¡De una!", "shortcut_id": None, "stage": "cierre"})
    assert parse_ai_reply(raw, valid_ids=frozenset()).stage == "cierre"
    raw = json.dumps({"message": "Hola", "stage": "loquesea"})
    assert parse_ai_reply(raw, valid_ids=frozenset()).stage == "conversando"


def test_broken_json_never_reaches_the_customer():
    raw = '{"message": "Claro, te cuento 😊", "stage": "interesado", '
    assert parse_ai_reply(raw, valid_ids=frozenset()).message == "Claro, te cuento 😊"


def test_interest_and_closing_signals():
    assert detect_interest_signal("¿Me pasas el catálogo?") is True
    assert detect_interest_signal("Voy para Cartagena") is False
    assert detect_interest_signal("Me interesa saber más") is False
    assert detect_closing_intent("Me interesa") is False
    assert detect_closing_intent("Listo, ¿cómo pago?") is True


def test_paso_only_counts_as_rejection_alone():
    assert classify_inbound_message("yo paso, gracias")["category"] == "no_interesado"
    assert classify_inbound_message("paso mañana por la tienda")["category"] != "no_interesado"


def test_book_slot_without_end_uses_the_free_block():
    keys = frozenset({("2026-10-06", "10:00", "11:00")})
    slot = parse_book_slot({"date": "2026-10-06", "start": "10:00"}, valid_keys=keys)
    assert slot == BookSlotRequest(date="2026-10-06", start="10:00", end="11:00", notes="")


def test_booking_prompt_groups_slots_and_mentions_existing():
    slots = [
        {"date": "2026-10-06", "start": "09:00", "end": "10:00", "label": "Mañana 09:00–10:00"},
        {"date": "2026-10-06", "start": "10:00", "end": "11:00", "label": "Mañana 10:00–11:00"},
        {"date": "2026-10-07", "start": "09:00", "end": "10:00", "label": "Mié 09:00–10:00"},
    ]
    out = append_appointment_instructions("Base", slots, existing_appointment="Mañana a las 15:00")
    assert "- Mañana (2026-10-06): 09:00, 10:00" in out
    assert "- Mié (2026-10-07): 09:00" in out
    assert "cada cita dura 60 min" in out
    assert "ya tiene cita: Mañana a las 15:00" in out


def test_stage_rules():
    from app.application.ai.ai_service import _resolve_stage

    def msg(body):
        return SimpleNamespace(body=body, transcript=None)

    sent_catalog = AiGeneratedReply(message="Te lo paso 👇", shortcut_id="cat-1")
    assert _resolve_stage(sent_catalog, message=msg("hola"), shortcuts=[CATALOG], booking=None) == "interesado"

    plain = AiGeneratedReply(message="Claro")
    assert _resolve_stage(plain, message=msg("Listo, ¿cómo pago?"), shortcuts=[], booking=None) == "cierre"
    assert _resolve_stage(plain, message=msg("¿Cuánto cuesta?"), shortcuts=[], booking=None) == "conversando"

    booking = BookingContext(free_slots=[])
    wants_slot = AiGeneratedReply(message="Tengo a las 3", stage="cierre")
    assert (
        _resolve_stage(wants_slot, message=msg("Quiero agendar una cita"), shortcuts=[], booking=booking)
        == "interesado"
    )


def _seed(db, *, body: str, profile_kwargs: dict | None = None, alert_recipients: list | None = None):
    from app.domain.entities import Conversation, Message, Tenant, TenantProfile, WhatsAppSession
    from app.domain.entities.enums import AiMode, MessageDirection, MessageSource, WhatsAppStatus

    tenant = Tenant(
        business_name="KatShoes Test",
        slug=f"t-{uuid.uuid4().hex[:8]}",
        ai_global_enabled=True,
        whatsapp_status=WhatsAppStatus.CONNECTED.value,
    )
    db.add(tenant)
    db.flush()
    db.add(
        TenantProfile(
            tenant_id=tenant.id,
            ai_mode=AiMode.QUALIFY.value,
            alert_recipients=alert_recipients or [],
            **(profile_kwargs or {}),
        )
    )
    db.add(
        WhatsAppSession(
            tenant_id=tenant.id,
            instance_name=f"inst_{uuid.uuid4().hex[:8]}",
            status=WhatsAppStatus.CONNECTED.value,
        )
    )
    conv = Conversation(
        tenant_id=tenant.id,
        contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
        contact_name="Laura",
        ai_active=True,
        mode="auto",
        bait_sent=True,
        whatsapp_connection_id=uuid.uuid4(),
    )
    db.add(conv)
    db.flush()
    msg = Message(
        tenant_id=tenant.id,
        conversation_id=conv.id,
        direction=MessageDirection.IN.value,
        source=MessageSource.CONTACT.value,
        body=body,
        status="received",
    )
    db.add(msg)
    db.commit()
    return tenant, conv, msg


def _run(db, tenant, conv, msg):
    from app.application.ai.ai_service import process_ai_reply

    ok = process_ai_reply(db, tenant_id=tenant.id, conversation_id=conv.id, message_id=msg.id)
    db.commit()
    db.refresh(conv)
    return ok


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_asking_for_catalog_marks_interested_and_keeps_ai_helping(_sleep, mock_generate, mock_send):
    from app.infrastructure.persistence.database import SessionLocal

    mock_generate.return_value = AiGeneratedReply(message="¡Claro! Te lo paso 👇", shortcut_id="cat-1")
    with SessionLocal() as db:
        tenant, conv, msg = _seed(
            db, body="¿Me puedes enviar el catálogo?", profile_kwargs={"quick_shortcuts": [CATALOG]}
        )
        assert _run(db, tenant, conv, msg) is True

    mock_send.assert_called_once()
    assert conv.interest_status == "interested"
    assert conv.mode == "auto"
    assert conv.ai_active is True


@requires_db
@patch("app.application.conversations.interest_alert_service.send_alert")
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_closing_hands_off_softly_and_alerts_owner_and_dispatch(_sleep, mock_generate, mock_send, mock_alert):
    from app.infrastructure.persistence.database import SessionLocal

    mock_generate.return_value = AiGeneratedReply(
        message="¡De una! Dame un momentico y te confirmo 🙌", stage="cierre"
    )
    with SessionLocal() as db:
        tenant, conv, msg = _seed(
            db,
            body="Me llevo los blancos en talla 38",
            alert_recipients=[
                {"name": "Dueña", "phone": "+573001234567", "scope": "all"},
                {"name": "Carlos", "phone": "+573005556644", "scope": "sales"},
            ],
        )
        assert _run(db, tenant, conv, msg) is True

    sent_reply = mock_send.call_args.kwargs["reply"]
    assert "asesor" not in sent_reply.message.lower()
    assert conv.interest_status == "interested"
    assert conv.mode == "manual"
    assert conv.ai_active is False
    assert sorted(c.args[1] for c in mock_alert.call_args_list) == ["+573001234567", "+573005556644"]
    text = mock_alert.call_args.args[2]
    assert "ya quiere comprar" in text
    assert "Me llevo los blancos en talla 38" in text
    assert "Cliente: Laura · " in text


def _tomorrow_iso() -> str:
    from app.application.appointments.appointment_service import BOGOTA

    return (datetime.now(BOGOTA).date() + timedelta(days=1)).isoformat()


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_books_a_free_slot_when_enabled(_sleep, mock_generate, mock_send):
    from app.domain.entities import Appointment
    from app.infrastructure.persistence.database import SessionLocal

    day = _tomorrow_iso()
    mock_generate.return_value = AiGeneratedReply(
        message="¡Listo! Te agendé mañana a las 10:00 ✅",
        book_slot=BookSlotRequest(date=day, start="10:00", end="11:00", notes="limpieza"),
        stage="interesado",
    )
    with SessionLocal() as db:
        tenant, conv, msg = _seed(
            db, body="Quiero una cita mañana a las 10", profile_kwargs={"ai_booking_enabled": True}
        )
        assert _run(db, tenant, conv, msg) is True
        booking = mock_generate.call_args.kwargs["booking"]
        assert any(s["date"] == day and s["start"] == "10:00" for s in booking.free_slots)
        rows = db.query(Appointment).filter(Appointment.conversation_id == conv.id).all()

    assert len(rows) == 1
    assert rows[0].notes == "limpieza"
    assert conv.interest_status == "interested"
    assert conv.mode == "auto"
    assert "agendé" in mock_send.call_args.kwargs["reply"].message


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_taken_slot_offers_alternatives_instead_of_confirming(_sleep, mock_generate, mock_send):
    from app.application.appointments.appointment_service import create_appointment, parse_slot_to_datetimes
    from app.domain.entities import Appointment
    from app.infrastructure.persistence.database import SessionLocal

    day = _tomorrow_iso()
    mock_generate.return_value = AiGeneratedReply(
        message="¡Listo! Te agendé ✅",
        book_slot=BookSlotRequest(date=day, start="10:00", end="11:00"),
    )
    with SessionLocal() as db:
        tenant, conv, msg = _seed(
            db, body="Quiero una cita mañana a las 10", profile_kwargs={"ai_booking_enabled": True}
        )
        starts, ends = parse_slot_to_datetimes(datetime.fromisoformat(day).date(), "10:00", "11:00")
        create_appointment(db, tenant_id=tenant.id, starts_at=starts, ends_at=ends, client_name="Otra")
        db.commit()
        assert _run(db, tenant, conv, msg) is True
        mine = db.query(Appointment).filter(Appointment.conversation_id == conv.id).count()

    assert mine == 0
    text = mock_send.call_args.kwargs["reply"].message
    assert "se acaba de ocupar" in text
    assert "agendé" not in text


@requires_db
@patch("app.application.ai.ai_service.send_reply_with_shortcut")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_booking_stays_off_by_default(_sleep, mock_generate, mock_send):
    from app.infrastructure.persistence.database import SessionLocal

    mock_generate.return_value = AiGeneratedReply(message="Hola 😊")
    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="hola")
        _run(db, tenant, conv, msg)
    assert mock_generate.call_args.kwargs["booking"] is None


@requires_db
def test_past_slots_of_today_are_not_offered():
    from app.application.appointments.appointment_service import BOGOTA, collect_upcoming_free_slots
    from app.domain.entities import Tenant, TenantProfile
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = Tenant(business_name="Spa", slug=f"spa-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        db.flush()
        db.add(TenantProfile(tenant_id=tenant.id))
        db.flush()
        now = datetime.now(BOGOTA).replace(hour=12, minute=10, second=0, microsecond=0)
        slots = collect_upcoming_free_slots(db, tenant_id=tenant.id, days=1, limit=50, now=now)
        db.rollback()

    assert slots
    assert slots[0]["start"] == "13:00"


@requires_db
def test_schedule_api_toggles_ai_booking(client: TestClient):
    tag = uuid.uuid4().hex[:8]
    res = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Spa {tag}",
            "owner_name": "Dueña",
            "email": f"spa-{tag}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
    assert client.get("/api/v1/appointments/schedule", headers=headers).json()["ai_booking_enabled"] is False

    saved = client.put(
        "/api/v1/appointments/schedule",
        headers=headers,
        json={"open_time": "09:00", "close_time": "17:00", "slot_minutes": 30, "ai_booking_enabled": True},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["ai_booking_enabled"] is True
    assert client.get("/api/v1/appointments/schedule", headers=headers).json()["ai_booking_enabled"] is True

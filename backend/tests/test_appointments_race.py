"""Dos clientes pidiendo la misma hora en el mismo instante: uno la gana, el otro recibe un aviso claro."""
from __future__ import annotations

import threading
from unittest.mock import patch

from tests.conftest import requires_db
from tests.test_staff_scheduling import _next_weekday, _owner_headers, _staff, _tenant


def _race(tenant_id, day, *, attempts: int = 2):
    from app.application.appointments.appointment_service import (
        SlotTakenError,
        create_appointment,
        parse_slot_to_datetimes,
    )
    from app.infrastructure.persistence.database import SessionLocal

    starts, ends = parse_slot_to_datetimes(day, "10:00", "11:00")
    start_line = threading.Barrier(attempts)
    won: list[str] = []
    lost: list[str] = []

    def _book(n: int) -> None:
        with SessionLocal() as db:
            start_line.wait()
            try:
                create_appointment(db, tenant_id=tenant_id, starts_at=starts, ends_at=ends, client_name=f"Cliente {n}")
                db.commit()
                won.append(f"Cliente {n}")
            except SlotTakenError as exc:
                db.rollback()
                lost.append(str(exc))

    threads = [threading.Thread(target=_book, args=(n,)) for n in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    return won, lost


def _count(tenant_id) -> int:
    from app.domain.entities import Appointment
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        return db.query(Appointment).filter(Appointment.tenant_id == tenant_id).count()


@requires_db
def test_same_slot_at_the_same_instant_only_one_wins():
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _tenant(db)
        db.commit()
        tenant_id = tenant.id

    won, lost = _race(tenant_id, _next_weekday(2), attempts=4)

    assert len(won) == 1
    assert lost == ["Ese horario ya está ocupado"] * 3
    assert _count(tenant_id) == 1


@requires_db
def test_with_one_barber_the_second_client_hears_nobody_is_free():
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant = _tenant(db)
        _staff(db, tenant, "Sebastián")
        db.commit()
        tenant_id = tenant.id

    won, lost = _race(tenant_id, _next_weekday(2))

    assert len(won) == 1
    assert lost == ["No hay nadie libre a esa hora"]
    assert _count(tenant_id) == 1


@requires_db
@patch("app.application.whatsapp.whatsapp_gateway.send_text")
def test_taken_slot_answers_409_so_the_panel_can_say_it_clearly(_wa, client):
    headers = _owner_headers(client)
    day = _next_weekday(3).isoformat()
    body = {"date": day, "start_time": "10:00", "end_time": "11:00", "client_name": "Ana"}
    first = client.post("/api/v1/appointments", json=body, headers=headers)
    assert first.status_code == 201, first.text
    second = client.post("/api/v1/appointments", json={**body, "client_name": "Beto"}, headers=headers)
    assert second.status_code == 409
    assert second.json()["detail"] == "Ese horario ya está ocupado"

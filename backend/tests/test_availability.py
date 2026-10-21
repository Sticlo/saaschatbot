"""Disponibilidad real: la IA lee el calendario iCal de cada alojamiento antes de ofrecer fechas."""
from __future__ import annotations

import socket
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.application.availability import availability_service as svc
from tests.conftest import requires_db

ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
DTSTART;VALUE=DATE:20261024\r
DTEND;VALUE=DATE:20261026\r
SUMMARY:Reserved\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;VALUE=DATE:20261030\r
DTEND;VALUE=DATE:20261031\r
STATUS:CANCELLED\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART:20261101T200000Z\r
DTEND:20261103T170000Z\r
DESCRIPTION:Reserva larga que\r
  sigue en otra línea\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;TZID=America/Bogota:20261110T100000\r
DTEND;TZID=America/Bogota:20261110T180000\r
END:VEVENT\r
END:VCALENDAR\r
"""

TODAY = date(2026, 10, 21)


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    def _broken():
        raise RuntimeError("sin redis en este test")

    monkeypatch.setattr(svc, "get_redis", _broken)


def test_parse_ics_reads_stays_and_skips_cancelled():
    ranges = svc.parse_ics(ICS)
    assert (date(2026, 10, 24), date(2026, 10, 26)) in ranges
    assert all(a != date(2026, 10, 30) for a, _ in ranges)
    # 20:00 UTC = 15:00 en Bogotá: llega el 1 y sale el 3.
    assert (date(2026, 11, 1), date(2026, 11, 3)) in ranges
    # Evento de unas horas: ocupa ese día.
    assert (date(2026, 11, 10), date(2026, 11, 11)) in ranges


def test_busy_days_and_format_group_consecutive_nights():
    days = svc.busy_days(svc.parse_ics(ICS), start=TODAY)
    assert days[:2] == [date(2026, 10, 24), date(2026, 10, 25)]
    assert svc.format_days(days) == "24–25 oct, 1–2 nov, 10 nov"
    assert svc.format_days([date(2026, 10, 31), date(2026, 11, 1)]) == "31 oct – 1 nov"


def test_block_lists_each_calendar_and_rules(monkeypatch):
    monkeypatch.setattr(svc, "_download", lambda url: ICS if "cabana" in url else "BEGIN:VCALENDAR\nEND:VCALENDAR")
    profile = SimpleNamespace(availability_calendars=[
        {"id": "1", "name": "Cabaña del lago", "url": "https://example.com/cabana.ics"},
        {"id": "2", "name": "Domo", "url": "https://example.com/domo.ics"},
    ])
    block = svc.availability_block(profile, today=TODAY)
    assert "- Cabaña del lago: ocupado 24–25 oct, 1–2 nov, 10 nov" in block
    assert "- Domo: todo libre" in block
    assert "Antes de decir que hay cupo" in block
    assert "No digas que la fecha quedó reservada" in block


def test_block_never_promises_dates_of_a_broken_calendar(monkeypatch):
    def fail(_url):
        raise svc.CalendarError("caído")

    monkeypatch.setattr(svc, "_download", fail)
    profile = SimpleNamespace(availability_calendars=[{"id": "1", "name": "Domo", "url": "https://example.com/d.ics"}])
    assert "Domo: no se pudo revisar su calendario. No confirmes fechas" in svc.availability_block(profile, today=TODAY)
    assert svc.availability_block(SimpleNamespace(availability_calendars=None)) == ""


def test_stale_copy_is_used_when_calendar_goes_down(monkeypatch):
    store: dict[str, str] = {}
    fake = SimpleNamespace(get=store.get, set=lambda key, value, ex: store.__setitem__(key, value))
    monkeypatch.setattr(svc, "get_redis", lambda: fake)
    monkeypatch.setattr(svc, "_download", lambda url: ICS)
    url = "https://example.com/c.ics"
    first, _, _ = svc.busy_ranges(url)
    store.pop(svc._cache_key(url, "fresh"))

    def fail(_url):
        raise svc.CalendarError("caído")

    monkeypatch.setattr(svc, "_download", fail)
    again, _, error = svc.busy_ranges(url)
    assert again == first and error == "caído"


@pytest.mark.parametrize("url", ["http://example.com/c.ics", "ftp://x/c.ics", "https://example.com:8080/c.ics", "c.ics"])
def test_only_https_links(url):
    with pytest.raises(svc.CalendarError):
        svc.normalize_url(url)


def test_web_page_is_not_a_calendar(monkeypatch):
    """Pegar la página del negocio no puede convertirse en «todo libre» para la IA."""
    import httpx

    real_client = httpx.Client
    page = "<!DOCTYPE html><html><body>Reserva ahora</body></html>"
    monkeypatch.setattr(svc, "_require_public_host", lambda host: None)
    monkeypatch.setattr(
        svc.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=page))),
    )
    with pytest.raises(svc.CalendarError, match="no es un calendario iCal"):
        svc._download("https://glamping.example.com")
    profile = SimpleNamespace(availability_calendars=[{"id": "1", "name": "Glamping", "url": "https://glamping.example.com"}])
    block = svc.availability_block(profile, today=TODAY)
    assert "todo libre" not in block
    assert "no se pudo revisar su calendario" in block


def test_webcal_becomes_https():
    assert svc.normalize_url(" webcal://example.com/c.ics ") == "https://example.com/c.ics"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "::1"])
def test_internal_addresses_are_blocked(monkeypatch, ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(svc.socket, "getaddrinfo", lambda *a, **k: [(family, 1, 6, "", (ip, 443))])
    with pytest.raises(svc.CalendarError, match="no es de un calendario público"):
        svc._download("https://calendario.example.com/c.ics")


def _register(client: TestClient) -> dict:
    tag = uuid.uuid4().hex[:8]
    res = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Glamping {tag}",
            "owner_name": "Dueña",
            "email": f"glamping-{tag}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    assert res.status_code == 201, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@requires_db
def test_owner_saves_and_checks_calendars(client: TestClient, monkeypatch):
    monkeypatch.setattr(svc, "_download", lambda url: ICS)
    headers = _register(client)

    saved = client.put(
        "/api/v1/availability/calendars",
        headers=headers,
        json={"calendars": [
            {"name": "  Cabaña   del lago ", "url": "webcal://example.com/cabana.ics"},
            {"name": "", "url": ""},
        ]},
    )
    assert saved.status_code == 200, saved.text
    rows = saved.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "Cabaña del lago"
    assert rows[0]["url"] == "https://example.com/cabana.ics"
    assert rows[0]["id"]

    listed = client.get("/api/v1/availability/calendars", headers=headers)
    assert listed.json() == rows

    bad = client.put(
        "/api/v1/availability/calendars",
        headers=headers,
        json={"calendars": [{"name": "Domo", "url": "http://example.com/d.ics"}]},
    )
    assert bad.status_code == 400

    check = client.post("/api/v1/availability/calendars/check", headers=headers, json={"url": "https://example.com/c.ics"})
    assert check.status_code == 200, check.text
    assert check.json()["ok"] is True

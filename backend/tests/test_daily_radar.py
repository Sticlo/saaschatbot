from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from tests.conftest import requires_db

NOW = datetime.now(timezone.utc)


def _store(db, *, name="Alfa Test", alerts_to_own=True, connected=True, created_days_ago=1):
    from app.domain.entities import Product, Tenant, TenantProfile, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus

    tenant = Tenant(
        business_name=name,
        slug=f"radar-{uuid.uuid4().hex[:8]}",
        whatsapp_status=WhatsAppStatus.CONNECTED.value if connected else WhatsAppStatus.DISCONNECTED.value,
        created_at=NOW - timedelta(days=created_days_ago),
    )
    db.add(tenant)
    db.flush()
    business = "+573001234567"
    db.add(
        WhatsAppSession(
            tenant_id=tenant.id,
            instance_name=f"i_{uuid.uuid4().hex[:8]}",
            phone_number=business if connected else None,
            last_connected_at=NOW - timedelta(hours=20) if connected else None,
        )
    )
    db.add(
        TenantProfile(
            tenant_id=tenant.id,
            payment_methods="Nequi 3000000000",
            alert_recipients=[{"name": "Yo", "phone": business if alerts_to_own else "+573009998877", "scope": "all"}],
        )
    )
    db.add(Product(tenant_id=tenant.id, name="Ropa Unisex", price_cop=60000, photos=[]))
    db.flush()
    db.refresh(tenant)
    return tenant


def _chat(db, tenant, turns):
    """turns: (minutos atrás, fuente, texto)."""
    from app.domain.entities import Conversation, Message

    conv = Conversation(
        tenant_id=tenant.id,
        contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
        contact_name="Thia",
        last_message_at=NOW,
    )
    db.add(conv)
    db.flush()
    for minutes, source, body in turns:
        db.add(
            Message(
                tenant_id=tenant.id,
                conversation_id=conv.id,
                direction="in" if source == "contact" else "out",
                source=source,
                body=body,
                status="received",
                created_at=NOW - timedelta(minutes=minutes),
            )
        )
    db.flush()
    return conv


@requires_db
def test_radar_catches_what_happened_to_alfa_store():
    from app.application.platform.daily_radar import build_radar, render_radar
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        alfa = _store(db)
        _chat(
            db,
            alfa,
            [
                (30, "contact", "Fotos"),
                (29, "bot", "¡Claro! Mira 👇"),
                (28, "contact", "??"),
                (27, "bot", "Ahí te van las fotos de nuestra Ropa Unisex"),
                (26, "contact", "Foto ?"),
            ],
        )
        _chat(
            db,
            alfa,
            [(20, "contact", "Fotos de la gorra"), (19, "bot", "¡Claro! Mira 👇"), (18.9, "bot", "[Imagen]\nGorra")],
        )
        never = _store(db, name="Nunca Conectó", connected=False, created_days_ago=3)
        fine = _store(db, name="Todo Bien", alerts_to_own=False)
        _chat(db, fine, [(10, "contact", "Hola, ¿cuánto vale?"), (9, "bot", "Vale $60.000. ¿Te la aparto?")])

        radar = build_radar(db, NOW, tenant_ids=[alfa.id, never.id, fine.id])
        subject, text = render_radar(radar, NOW)
        db.rollback()

    row = radar.tenants[alfa.id]
    assert len(row.frustrated) == 1 and row.frustrated[0].startswith("Thia (…")
    assert row.frustrated[0].endswith("«??»")
    assert len(row.broken_promises) == 2
    assert "Las alertas van al mismo número del negocio: no le suenan" in row.setup
    assert "Ningún producto tiene fotos" in row.setup
    assert radar.tenants[never.id].status == ["Se registró hace 3 días y nunca conectó WhatsApp"]
    assert not radar.tenants.get(fine.id) or not radar.tenants[fine.id].frustrated
    assert subject.endswith("3 cosas por revisar")
    assert "CLIENTES FRUSTRADOS" in text and "PROMETIÓ" in text and "Nunca Conectó" in text


@requires_db
def test_radar_is_sent_once_a_day_from_seven(monkeypatch):
    from app.application.appointments.appointment_service import BOGOTA
    from app.application.platform import daily_radar
    from app.infrastructure.cache.redis_client import get_redis
    from app.infrastructure.email import email_service
    from app.infrastructure.persistence.database import SessionLocal

    sent: list[str] = []
    monkeypatch.setattr(daily_radar.settings, "ops_alert_email", "juan@test.com")
    monkeypatch.setattr(daily_radar.settings, "platform_admin_emails", "")
    monkeypatch.setattr(email_service, "send_ops_alert_email", lambda **kw: sent.append(kw["subject"]))
    monkeypatch.setattr(daily_radar, "build_radar", lambda db, now: daily_radar.Radar(since=now))

    early = datetime(2026, 10, 15, 6, 30, tzinfo=BOGOTA)
    later = datetime(2026, 10, 15, 7, 15, tzinfo=BOGOTA)
    get_redis().delete("platform:daily_radar:2026-10-15")
    with SessionLocal() as db:
        assert daily_radar.send_daily_radar(db, early) is False
        assert daily_radar.send_daily_radar(db, later) is True
        assert daily_radar.send_daily_radar(db, later + timedelta(minutes=15)) is False
    assert sent == ["Radar Omitel 15/10: todo en orden"]

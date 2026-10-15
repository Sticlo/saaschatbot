from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tests.conftest import requires_db
from tests.test_phase1 import _register


def _summary(items="2 camisetas negras talla M", *, total="$80.000", payment="Transferencia Nequi", paid=False):
    from app.application.ai.ai_shortcut_service import OrderSummary

    return OrderSummary(
        items=items,
        total=total,
        customer="Laura Gómez, CC 1020",
        delivery="Calle 10 #20-30, Medellín",
        payment=payment,
        paid=paid,
        lines=(),
    )


def _tenant_and_chat(db, tenant_id=None):
    from app.domain.entities import Conversation, Tenant, WhatsAppSession
    from app.domain.entities.enums import WhatsAppStatus

    if tenant_id is None:
        tenant = Tenant(
            business_name="Tienda Ropa",
            slug=f"ropa-{uuid.uuid4().hex[:8]}",
            whatsapp_status=WhatsAppStatus.CONNECTED.value,
        )
        db.add(tenant)
        db.flush()
    else:
        tenant = db.get(Tenant, tenant_id)
        tenant.whatsapp_status = WhatsAppStatus.CONNECTED.value
    db.add(
        WhatsAppSession(
            tenant_id=tenant.id,
            instance_name=f"inst_{uuid.uuid4().hex[:8]}",
            status=WhatsAppStatus.CONNECTED.value,
        )
    )
    conversation = Conversation(
        tenant_id=tenant.id,
        contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
        contact_name="Laura",
        last_message_at=datetime.now(timezone.utc),
    )
    db.add(conversation)
    db.flush()
    db.refresh(tenant)
    return tenant, conversation


@requires_db
def test_the_ai_repeating_the_order_updates_it_instead_of_duplicating():
    from app.application.orders.order_service import record_ai_order
    from app.domain.entities import Order
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, chat = _tenant_and_chat(db)
        first = record_ai_order(db, tenant, chat, _summary())
        assert first.status == "pending" and first.contact_name == "Laura"

        again = record_ai_order(db, tenant, chat, _summary(paid=True))
        assert again.id == first.id
        assert again.status == "ready" and again.payment_proof is True
        assert db.query(Order).filter(Order.conversation_id == chat.id).count() == 1

        # Nunca vuelve atrás aunque la IA repita el pedido sin el comprobante.
        assert record_ai_order(db, tenant, chat, _summary()).status == "ready"
        db.rollback()


@requires_db
def test_cash_on_delivery_is_ready_to_dispatch_without_payment():
    from app.application.orders.order_service import record_ai_order
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, chat = _tenant_and_chat(db)
        order = record_ai_order(db, tenant, chat, _summary(payment="Contraentrega en efectivo"))
        assert order.status == "ready" and order.payment_proof is False
        db.rollback()


@requires_db
def test_after_dispatch_the_echo_is_ignored_but_a_new_purchase_counts():
    from app.application.orders.order_service import record_ai_order
    from app.domain.entities import Order
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, chat = _tenant_and_chat(db)
        order = record_ai_order(db, tenant, chat, _summary(paid=True))
        order.status = "dispatched"
        db.flush()

        assert record_ai_order(db, tenant, chat, _summary(paid=True)) is None
        second = record_ai_order(db, tenant, chat, _summary("1 gorra blanca", total="$35.000"))
        assert second.id != order.id and second.status == "pending"

        order.created_at = datetime.now(timezone.utc) - timedelta(days=5)
        second.status = "cancelled"
        db.flush()
        third = record_ai_order(db, tenant, chat, _summary())
        assert third is not None and third.id not in (order.id, second.id)
        assert db.query(Order).filter(Order.conversation_id == chat.id).count() == 3
        db.rollback()


@requires_db
def test_the_ai_knows_where_the_customers_order_is():
    from app.application.orders.order_service import orders_prompt_block, record_ai_order
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, chat = _tenant_and_chat(db)
        assert orders_prompt_block(db, chat.id) == ""
        order = record_ai_order(db, tenant, chat, _summary(paid=True))
        order.status = "dispatched"
        order.tracking = "SERV-123"
        db.flush()
        block = orders_prompt_block(db, chat.id)
        assert "2 camisetas negras talla M: despachado (guía SERV-123)" in block
        db.rollback()


@requires_db
def test_dispatching_notifies_the_customer_as_the_ai(client: TestClient, monkeypatch):
    from app.application.orders.order_service import record_ai_order
    from app.application.whatsapp import whatsapp_service
    from app.infrastructure.persistence.database import SessionLocal

    sent: list[dict] = []
    monkeypatch.setattr(whatsapp_service, "send_text_message", lambda db, **kw: sent.append(kw))

    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    tenant_id = uuid.UUID(client.get("/api/v1/tenants/me", headers=headers).json()["id"])
    with SessionLocal() as db:
        tenant, chat = _tenant_and_chat(db, tenant_id)
        order_id = str(record_ai_order(db, tenant, chat, _summary(paid=True)).id)
        db.commit()

    [listed] = client.get("/api/v1/orders", headers=headers).json()
    assert listed["id"] == order_id and listed["status"] == "ready" and listed["payment_proof"] is True

    r = client.patch(
        f"/api/v1/orders/{order_id}",
        json={"status": "dispatched", "tracking": "SERV-123"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "dispatched" and body["tracking"] == "SERV-123"
    assert body["customer_notified"] is True and body["dispatched_at"]
    [message] = sent
    assert message["source"] == "bot"
    assert message["text"].startswith("¡Hola Laura! Tu pedido ya va en camino: 2 camisetas negras talla M.")
    assert "SERV-123" in message["text"]

    quiet = client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "dispatched", "notify_customer": False}, headers=headers
    )
    assert quiet.json()["customer_notified"] is False and len(sent) == 1
    assert client.patch(f"/api/v1/orders/{order_id}", json={"status": "lost"}, headers=headers).status_code == 422


@requires_db
def test_a_business_never_sees_another_business_orders(client: TestClient):
    from app.application.orders.order_service import record_ai_order
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, chat = _tenant_and_chat(db)
        order_id = str(record_ai_order(db, tenant, chat, _summary()).id)
        db.commit()

    other = {"Authorization": f"Bearer {_register(client)['access_token']}"}
    assert client.get("/api/v1/orders", headers=other).json() == []
    assert client.patch(f"/api/v1/orders/{order_id}", json={"status": "cancelled"}, headers=other).status_code == 404


@requires_db
def test_no_followup_to_someone_who_already_bought():
    from app.application.ai.ai_followup_service import due_followups
    from app.application.orders.order_service import record_ai_order
    from app.infrastructure.persistence.database import SessionLocal
    from tests.test_ai_followup import NOW, _chat, _setup

    with SessionLocal() as db:
        tenant = _setup(db)
        bought = _chat(db, tenant, "Compró", history=("contact", "bot"), silent_hours=5)
        _chat(db, tenant, "Solo preguntó", history=("contact", "bot"), silent_hours=5)
        pending = _chat(db, tenant, "Falta pagar", history=("contact", "bot"), silent_hours=5)
        record_ai_order(db, tenant, bought, _summary(paid=True))
        record_ai_order(db, tenant, pending, _summary())
        assert {c.contact_name for c in due_followups(db, tenant, NOW)} == {"Solo preguntó", "Falta pagar"}
        db.rollback()

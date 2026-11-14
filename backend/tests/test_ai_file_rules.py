"""«El personaje de Pan solo va en la opción 3»: las letras pequeñas de cada PDF, y el asesor que
responde una vez para que la IA le confirme al cliente que quedó esperando."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.application.ai.ai_shortcut_service import append_shortcuts_instructions
from app.application.outbound.quick_shortcut_service import _normalize
from tests.conftest import requires_db

PAN_RULE = "El personaje de Pan (Dragon Ball) solo está en la Opción 3; en la 1 y la 2 no, por costo adicional."


def test_file_rules_are_saved_and_always_reach_the_ai():
    saved = _normalize(
        [
            {"label": "Infantil", "type": "document", "file_path": "t/infantil.pdf", "content": "x" * 9000, "ai_notes": PAN_RULE},
            {"label": "Vacío", "type": "document", "file_path": "t/otro.pdf", "ai_notes": "Solo para Bogotá"},
        ]
    )
    assert saved[0]["ai_notes"] == PAN_RULE

    prompt = append_shortcuts_instructions("Base", saved)
    assert "Reglas de este archivo (dadas por el dueño, mandan sobre lo demás):\n" + PAN_RULE in prompt
    assert "[Vacío]\nReglas de este archivo" in prompt and "Solo para Bogotá" in prompt
    assert "ni el\narchivo ni sus reglas dicen que ESA opción lo incluye" in prompt


def test_without_rules_the_catalog_reads_as_before():
    saved = _normalize([{"label": "Menú", "type": "document", "file_path": "t/m.pdf", "content": "Pizza $20.000"}])
    assert "ai_notes" not in saved[0]
    prompt = append_shortcuts_instructions("Base", saved)
    assert "[Menú]\nPizza $20.000" in prompt and "Reglas de este archivo (" not in prompt


def _setup(db, *, inbound_ago: timedelta):
    from app.domain.entities import AiQuestion, Conversation, Message, Tenant, WhatsAppSession

    now = datetime.now(timezone.utc)
    tenant = Tenant(
        business_name="Rino", slug=f"rino-{uuid.uuid4().hex[:8]}", ai_global_enabled=True, whatsapp_status="connected"
    )
    db.add(tenant)
    db.flush()
    db.add(WhatsAppSession(tenant_id=tenant.id, instance_name=f"i_{uuid.uuid4().hex[:8]}"))
    conversation = Conversation(tenant_id=tenant.id, contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}")
    db.add(conversation)
    db.flush()
    db.add(
        Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            direction="in",
            source="contact",
            body="¿La opción 2 puede ser de Pan de Dragon Ball?",
            status="received",
            created_at=now - inbound_ago,
        )
    )
    row = AiQuestion(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        topic="Personaje Pan en la opción 2",
        topic_key=f"pan {uuid.uuid4().hex[:6]}",
        question="¿La opción 2 puede ser de Pan?",
        status="answered",
        answer=PAN_RULE,
        times_asked=1,
        last_asked_at=now - inbound_ago,
    )
    db.add(row)
    db.flush()
    return tenant, conversation, row


@requires_db
def test_owner_answer_goes_back_to_the_waiting_client(monkeypatch):
    from app.application.ai import ai_service
    from app.application.ai.ai_answer_relay_service import relay_target
    from app.domain.entities import Message
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr(ai_service, "should_ai_respond", lambda *_a: True)
    with SessionLocal() as db:
        _, conversation, row = _setup(db, inbound_ago=timedelta(minutes=20))
        assert relay_target(db, row) is conversation

        db.add(
            Message(
                tenant_id=row.tenant_id,
                conversation_id=conversation.id,
                direction="out",
                source="agent",
                body="Hola, te cuento que Pan solo va en la opción 3",
                status="sent",
            )
        )
        db.flush()
        assert relay_target(db, row) is None
        db.rollback()


@requires_db
def test_no_message_outside_the_whatsapp_window_or_after_a_no(monkeypatch):
    from app.application.ai import ai_service
    from app.application.ai.ai_answer_relay_service import relay_target
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr(ai_service, "should_ai_respond", lambda *_a: True)
    with SessionLocal() as db:
        _, _, old = _setup(db, inbound_ago=timedelta(hours=30))
        assert relay_target(db, old) is None

        _, conversation, row = _setup(db, inbound_ago=timedelta(minutes=5))
        conversation.interest_status = "not_interested"
        assert relay_target(db, row) is None
        db.rollback()


def test_relay_message_keeps_the_owner_answer(monkeypatch):
    from types import SimpleNamespace

    from app.application.ai import ai_answer_relay_service as relay

    captured = {}

    def fake_completion(messages, **_kw):
        captured["messages"] = messages
        return "Te confirmo: Pan va en la Opción 3. ¿Te la separo?"

    monkeypatch.setattr(relay, "chat_completion", fake_completion)
    monkeypatch.setattr(relay, "build_system_prompt", lambda *_a: "Base")
    row = SimpleNamespace(topic="Personaje Pan en la opción 2", answer=PAN_RULE)
    text = relay.compose_relay(SimpleNamespace(profile=None), SimpleNamespace(), [], row)
    assert text.startswith("Te confirmo")
    assert PAN_RULE in captured["messages"][-1]["content"]

    monkeypatch.setattr(relay, "chat_completion", lambda *_a, **_kw: "NO")
    assert relay.compose_relay(SimpleNamespace(profile=None), SimpleNamespace(), [], row) is None


FILES = [
    {"id": "bs", "label": "Baby Shower", "type": "document", "file_path": "t/bs.pdf"},
    {"id": "inf", "label": "Infantil", "type": "document", "file_path": "t/inf.pdf"},
    {"id": "bod", "label": "Bodas", "type": "document", "file_path": "t/bod.pdf"},
]


def _reply(message: str):
    from app.application.ai.ai_shortcut_service import AiGeneratedReply

    return AiGeneratedReply(message=message)


def test_a_promised_pdf_is_attached_when_it_is_clear_which():
    from app.application.ai.ai_shortcut_service import attach_promised_media

    fixed = attach_promised_media(
        _reply("¡Claro! Te envío el PDF de infantil para que veas las opciones."),
        client_text="tienes opciones para fiesta infantil?", products=[], shortcuts=FILES,
    )
    assert fixed.shortcut_id == "inf"

    single = attach_promised_media(
        _reply("Listo, ya te envío."), client_text="me pasas el pdf?", products=[], shortcuts=FILES[:1]
    )
    assert single.shortcut_id == "bs"


def test_a_promised_pdf_without_knowing_which_asks_instead_of_lying():
    from app.application.ai.ai_shortcut_service import attach_promised_media

    fixed = attach_promised_media(
        _reply("¡Con gusto! Te envío el PDF con todas las opciones."),
        client_text="mándame el pdf", products=[], shortcuts=FILES,
    )
    assert fixed.shortcut_id is None
    assert fixed.message == "¡Con gusto! ¿Cuál te envío: Baby Shower, Infantil o Bodas?"


def test_photo_picked_after_asking_which_one_is_attached():
    import uuid
    from types import SimpleNamespace

    from app.application.ai.ai_shortcut_service import attach_promised_media
    from app.application.products.product_service import product_ref

    single = SimpleNamespace(id=uuid.uuid4(), name="Habitación Sencilla", photos=["a.jpg"])
    family = SimpleNamespace(id=uuid.uuid4(), name="Habitación Familiar", photos=["b.jpg"])
    fixed = attach_promised_media(
        _reply("¡Listo! Mira la Sencilla 👇 Son $45.000 la noche para 1 persona."),
        client_text="De una sencilla es para mi solo",
        products=[single, family],
        shortcuts=[],
        recent=(
            "De una sencilla es para mi solo",
            "¡Claro! ¿De cuál quieres ver fotos: Habitación Sencilla o Habitación Familiar? 📸",
            "Puedes enviarme fotos?",
        ),
    )
    assert fixed.photo_ref == product_ref(single)


def test_asking_for_all_photos_sends_every_product():
    import uuid
    from types import SimpleNamespace

    from app.application.ai.ai_shortcut_service import PHOTOS_ALL, attach_promised_media, parse_ai_reply

    single = SimpleNamespace(id=uuid.uuid4(), name="Habitación Sencilla", photos=["a.jpg"])
    family = SimpleNamespace(id=uuid.uuid4(), name="Habitación Familiar", photos=["b.jpg"])
    fixed = attach_promised_media(
        _reply("¡Claro! Mira 👇"),
        client_text="Muéstrame todas las fotos de habitación en general",
        products=[single, family],
        shortcuts=[],
    )
    assert fixed.photo_ref == PHOTOS_ALL

    parsed = parse_ai_reply(
        '{"message":"¡Claro! Mira 👇","photo_ref":"todas"}', valid_ids=frozenset(), valid_photo_refs=frozenset({"x"})
    )
    assert parsed.photo_ref == PHOTOS_ALL
    assert parse_ai_reply('{"message":"Hola","photo_ref":"todas"}', valid_ids=frozenset()).photo_ref is None


def test_all_photos_go_out_again_when_asked_but_not_on_a_retry(monkeypatch):
    from app.application.ai import ai_shortcut_service as svc

    reply = svc.AiGeneratedReply(message="Mira 👇", photo_ref=svc.PHOTOS_ALL)
    monkeypatch.setattr(svc, "photos_sent_at", lambda conv, ref: None)
    assert svc.wants_photos_again("muéstrame todas las fotos", reply, conversation_id="c", asked_at=100.0)
    monkeypatch.setattr(svc, "photos_sent_at", lambda conv, ref: 150.0)
    assert not svc.wants_photos_again("muéstrame todas las fotos", reply, conversation_id="c", asked_at=100.0)


def test_a_written_price_is_not_mistaken_for_a_file():
    from app.application.ai.ai_shortcut_service import attach_promised_media

    reply = _reply("Aquí tienes: la opción 2 vale $450.000 e incluye 2 recreadores.")
    assert attach_promised_media(reply, client_text="cuanto vale la 2", products=[], shortcuts=FILES) == reply


def test_file_without_text_never_shows_json_to_the_client():
    from app.application.ai.ai_shortcut_service import ATTACHMENT_ONLY_TEXT, parse_ai_reply

    raw = '{"message":"","shortcut_id":"b3c6","book_slot":null,"stage":"interesado","missing":"","chosen":"","notes":null,"order":null}'
    reply = parse_ai_reply(raw, valid_ids=frozenset({"b3c6"}))
    assert reply.shortcut_id == "b3c6" and reply.message == ATTACHMENT_ONLY_TEXT
    assert "{" not in reply.message


def test_empty_json_counts_as_no_reply_not_as_text():
    from app.application.ai.ai_shortcut_service import parse_ai_reply

    reply = parse_ai_reply('{"message":"","shortcut_id":"inventado","stage":"interesado"}', valid_ids=frozenset())
    assert reply.message == ""


def test_free_theme_is_not_a_free_pass_for_special_requests():
    from app.application.ai.ai_shortcut_service import _CATALOG_BLOCK

    assert "temática libre" in _CATALOG_BLOCK and "costo adicional" in _CATALOG_BLOCK

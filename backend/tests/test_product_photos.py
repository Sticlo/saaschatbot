"""Fotos de productos: la IA las manda cuando el cliente pide verlo, y el dueño las sube una vez."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.application.ai.ai_shortcut_service import (
    AiGeneratedReply,
    infer_photo_ref,
    parse_ai_reply,
    reply_format_instruction,
)
from app.application.products.product_service import product_ref, products_prompt_block
from app.domain.entities import Message, Product
from tests.conftest import requires_db
from tests.test_phase1 import _register
from tests.test_sales_stage_and_booking import _run, _seed

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _product(name, photos=(), price=None):
    return Product(id=uuid.uuid4(), name=name, price_cop=price, stock=None, photos=list(photos))


def test_prompt_marks_products_with_photos():
    suite = _product("Suite presidencial", ["assets/t/a.jpg", "assets/t/b.jpg"], 450000)
    block = products_prompt_block([suite, _product("Habitación doble")], selling=False)
    assert "| Suite presidencial | $450.000 | disponible | 📷 2 fotos" in block
    assert "pon su ref en photo_ref" in block
    assert "Habitación doble | precio por confirmar | disponible\n" in block

    without = products_prompt_block([_product("Habitación doble")], selling=False)
    assert "photo_ref" not in without
    assert "no prometas enviarlas" in without

    assert '"photo_ref":null' in reply_format_instruction(has_shortcuts=False, booking_enabled=False, has_photos=True)
    assert "photo_ref" not in reply_format_instruction(has_shortcuts=False, booking_enabled=False)


def test_photo_ref_only_for_products_with_photos():
    raw = json.dumps({"message": "¡Claro! Mira 👇", "photo_ref": "ref=A1B2C3D4"})
    assert parse_ai_reply(raw, valid_ids=frozenset(), valid_photo_refs=frozenset({"a1b2c3d4"})).photo_ref == "a1b2c3d4"
    assert parse_ai_reply(raw, valid_ids=frozenset()).photo_ref is None


def test_photos_are_attached_when_the_ai_forgets_the_ref():
    suite = _product("Suite presidencial", ["assets/t/a.jpg"])
    doble = _product("Habitación doble", ["assets/t/b.jpg"])
    sin_fotos = _product("Suite junior")
    products = [suite, doble, sin_fotos]

    assert infer_photo_ref("¡Claro! Mira 👇", "¿Tienes fotos de la suite?", products) == product_ref(suite)
    assert infer_photo_ref("Te las paso 📸", "fotos de la habitación doble porfa", products) == product_ref(doble)
    # Sin pedir fotos, o sin decir que las muestra, no se manda nada.
    assert infer_photo_ref("¡Claro! Mira 👇", "¿Cuánto vale la suite?", products) is None
    assert infer_photo_ref("La suite tiene jacuzzi y vista al mar", "¿Tienes fotos de la suite?", products) is None
    # Si no queda claro de cuál, mejor nada.
    assert infer_photo_ref("¡Claro! Mira 👇", "¿Tienes fotos?", products) is None


def test_plain_fotos_are_of_the_product_they_were_talking_about():
    """Alfa_Store: pidió «Fotos» tras ver el catálogo, la IA dijo «Mira 👇» tres veces y no salió nada."""
    from app.application.ai.ai_shortcut_service import photo_choice_message, wants_photos_again

    ropa = _product("Ropa Unisex", ["assets/t/a.jpg"], 60000)
    buzos = _product("Buzos hombre", ["assets/t/b.jpg"], 60000)
    products = [ropa, buzos]
    catalog = "¡Con gusto! Mira un adelanto 👇 - Ropa Unisex | $60.000 | tallas S, M, L ¿Te la muestro en fotos?"

    assert infer_photo_ref("¡Claro! Mira 👇", "Fotos", products, ("Fotos", catalog)) == product_ref(ropa)
    assert infer_photo_ref("¡Claro! Mira 👇", "Foto ?", products, ("Foto ?", "¡Perfecto! La talla M", "M", catalog)) == (
        product_ref(ropa)
    )
    # «??» reclamando las fotos prometidas, con la ref que la IA escribió en el texto.
    promised = f"Ahí te van las fotos de nuestra Ropa Unisex (ref {product_ref(ropa)}) 😊"
    assert infer_photo_ref(promised, "??", products) == product_ref(ropa)
    assert infer_photo_ref("¡Claro! Mira 👇", "fotos de los buzos", products, (catalog,)) == product_ref(buzos)
    # Con un solo producto con fotos no hay duda.
    assert infer_photo_ref("¡Claro! Mira 👇", "¿Tienes fotos?", [ropa, _product("Gorra")]) == product_ref(ropa)

    assert photo_choice_message(products) == "¡Claro! ¿De cuál quieres ver fotos: Ropa Unisex o Buzos hombre? 📸"
    assert wants_photos_again("no me llegó", AiGeneratedReply(message="Mira 👇"), conversation_id=uuid.uuid4(), asked_at=0)
    assert not wants_photos_again("Foto ?", AiGeneratedReply(message="¿De cuál?"), conversation_id=uuid.uuid4(), asked_at=0)


def test_the_client_never_sees_the_internal_ref():
    from app.application.ai.ai_shortcut_service import without_internal_refs

    ropa = _product("Ropa Unisex", ["assets/t/a.jpg"], 60000)
    ref = product_ref(ropa)
    assert without_internal_refs(f"Fotos de nuestra Ropa Unisex (ref {ref}), en talla M.", [ropa]) == (
        "Fotos de nuestra Ropa Unisex, en talla M."
    )
    assert without_internal_refs(f"La Ropa Unisex ref={ref} vale $60.000", [ropa]) == "La Ropa Unisex vale $60.000"
    # Una referencia del negocio que no es nuestra ref se queda.
    assert without_internal_refs("Es la referencia 20491234", [ropa]) == "Es la referencia 20491234"


@pytest.fixture
def assets_tmp(tmp_path, monkeypatch):
    from app.application.outbound import tenant_asset_service

    monkeypatch.setattr(tenant_asset_service, "_ASSETS_ROOT", tmp_path)
    return tmp_path


def _upload(client, headers, url, content=PNG, name="suite.png", data=None):
    return client.post(url, headers=headers, files={"file": (name, content, "image/png")}, data=data or {})


@requires_db
def test_owner_uploads_up_to_four_photos(client: TestClient, assets_tmp):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    product_id = client.post("/api/v1/products", json={"name": "Suite", "price_cop": 450000}, headers=headers).json()["id"]
    url = f"/api/v1/products/{product_id}/photos"

    r = _upload(client, headers, url, content=b"no soy una foto")
    assert r.status_code == 400

    for n in range(1, 5):
        r = _upload(client, headers, url)
        assert r.status_code == 200, r.text
        assert len(r.json()["photos"]) == n
    r = _upload(client, headers, url)
    assert r.status_code == 422 and "hasta 4 fotos" in r.json()["detail"]

    name = client.get("/api/v1/products", headers=headers).json()[0]["photos"][0]
    tenant_dir = next(assets_tmp.iterdir())
    assert (tenant_dir / name).is_file()
    assert client.get(f"/api/v1/outbound/assets/{name}", headers=headers).status_code == 200

    r = client.delete(f"{url}/{name}", headers=headers)
    assert r.status_code == 200 and name not in r.json()["photos"] and len(r.json()["photos"]) == 3
    assert not (tenant_dir / name).exists()

    other = _register(client)
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert _upload(client, other_headers, url).status_code == 404

    assert client.delete(f"/api/v1/products/{product_id}", headers=headers).status_code == 204
    assert list(tenant_dir.iterdir()) == []


@requires_db
def test_pending_question_answered_with_a_photo(client: TestClient, assets_tmp):
    from app.application.ai.ai_knowledge_service import learned_answers_block, record_missing_info
    from app.infrastructure.persistence.database import SessionLocal

    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    tenant_id = uuid.UUID(client.get("/api/v1/tenants/me", headers=headers).json()["id"])
    product_id = client.post("/api/v1/products", json={"name": "Suite", "price_cop": 450000}, headers=headers).json()["id"]
    with SessionLocal() as db:
        row, _ = record_missing_info(
            db, tenant_id=tenant_id, conversation_id=None, topic="fotos de la suite", question="tienes fotos de la suite?"
        )
        db.commit()
        question_id = row.id

    r = _upload(client, headers, f"/api/v1/ai-questions/{question_id}/photo", data={"product_id": product_id})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "answered" and "«Suite»" in r.json()["answer"]
    assert len(client.get("/api/v1/products", headers=headers).json()[0]["photos"]) == 1
    with SessionLocal() as db:
        assert "fotos de la suite" in learned_answers_block(db, tenant_id)


@requires_db
@patch("app.application.ai.ai_shortcut_service.send_image_message")
@patch("app.application.ai.ai_shortcut_service.send_text_message")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_ai_sends_the_product_photos(_sleep, mock_generate, mock_text, mock_image):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="Hola, ¿tienes fotos de la suite?")
        suite = Product(
            tenant_id=tenant.id,
            name="Suite presidencial",
            price_cop=450000,
            photos=[f"assets/{tenant.id}/a.jpg", f"assets/{tenant.id}/b.jpg"],
        )
        db.add(suite)
        db.commit()

        # La IA dice «mira» pero olvida photo_ref: igual salen las fotos.
        mock_generate.return_value = AiGeneratedReply(message="¡Claro! Mira 👇")
        assert _run(db, tenant, conv, msg) is True
        assert mock_text.call_args.kwargs["text"] == "¡Claro! Mira 👇"
        sent = [c.kwargs for c in mock_image.call_args_list]
        assert [s["image_path"] for s in sent] == suite.photos
        assert [s["caption"] for s in sent] == ["Suite presidencial — $450.000", ""]
        assert conv.interest_status == "interested"

        # Repetir enseguida no las vuelve a mandar.
        mock_generate.return_value = AiGeneratedReply(message="Ahí te las dejé 👆", photo_ref=product_ref(suite))
        assert _run(db, tenant, conv, msg) is True
        assert mock_image.call_count == 2

        # Si el cliente las vuelve a pedir después de recibirlas, salen otra vez.
        again = Message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            direction="in",
            source="contact",
            body="Foto ?",
            status="received",
            created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
        )
        db.add(again)
        db.commit()
        mock_generate.return_value = AiGeneratedReply(message="¡Claro! Mira 👇", photo_ref=product_ref(suite))
        assert _run(db, tenant, conv, again) is True
        assert mock_image.call_count == 4


@requires_db
@patch("app.application.ai.ai_shortcut_service.send_image_message")
@patch("app.application.ai.ai_shortcut_service.send_text_message")
@patch("app.application.ai.ai_service.generate_qualify_reply")
@patch("app.application.ai.ai_service.time.sleep", return_value=None)
def test_plain_fotos_never_end_in_an_empty_mira(_sleep, mock_generate, mock_text, mock_image):
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        tenant, conv, msg = _seed(db, body="Fotos")
        ropa = Product(tenant_id=tenant.id, name="Ropa Unisex", price_cop=60000, photos=[f"assets/{tenant.id}/a.jpg"])
        buzos = Product(tenant_id=tenant.id, name="Buzos hombre", price_cop=60000, photos=[f"assets/{tenant.id}/b.jpg"])
        db.add_all([ropa, buzos])
        db.commit()

        # Sin haber hablado de ningún producto: se pregunta de cuál en vez de un «Mira 👇» vacío.
        mock_generate.return_value = AiGeneratedReply(message="¡Claro! Mira 👇")
        assert _run(db, tenant, conv, msg) is True
        assert mock_text.call_args.kwargs["text"] == "¡Claro! ¿De cuál quieres ver fotos: Ropa Unisex o Buzos hombre? 📸"
        assert mock_image.call_count == 0

        db.add(
            Message(
                tenant_id=tenant.id,
                conversation_id=conv.id,
                direction="out",
                source="bot",
                body="Tenemos la Ropa Unisex a $60.000 en tallas S a XL",
                status="sent",
                created_at=datetime.now(timezone.utc) + timedelta(seconds=2),
            )
        )
        again = Message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            direction="in",
            source="contact",
            body="Foto ?",
            status="received",
            created_at=datetime.now(timezone.utc) + timedelta(seconds=4),
        )
        db.add(again)
        db.commit()
        ref = product_ref(ropa)
        mock_generate.return_value = AiGeneratedReply(message=f"¡Claro! Mira 👇 (ref {ref})")
        assert _run(db, tenant, conv, again) is True
        assert mock_text.call_args.kwargs["text"] == "¡Claro! Mira 👇"
        assert [c.kwargs["image_path"] for c in mock_image.call_args_list] == ropa.photos

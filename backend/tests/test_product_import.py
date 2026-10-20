"""Inventario completo en un minuto: el dueño sube su Excel, Word o PDF y la IA conoce todos sus productos."""
from __future__ import annotations

import io
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application.products.product_import_service import parse_price, parse_upload
from app.application.products.product_service import products_prompt_block, rank_products
from app.domain.entities import Product
from tests.conftest import requires_db
from tests.test_phase1 import _register
from tests.test_platform_console import admin_and_client  # noqa: F401


def _xlsx(rows: list[list]) -> bytes:
    shared: list[str] = []
    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if isinstance(value, str):
                if value not in shared:
                    shared.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{shared.index(value)}</v></c>')
            elif value is not None:
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{''.join(sheet_rows)}</sheetData></worksheet>")
    return buf.getvalue()


def _docx(paragraphs: list[str], table: list[list[str]]) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    rows = "".join(
        "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row) + "</w:tr>"
        for row in table
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {w}><w:body>{body}<w:tbl>{rows}</w:tbl></w:body></w:document>")
    return buf.getvalue()


INVENTORY_XLSX = [
    ["Mi tienda — inventario", None, None, None, None, None],
    ["Código", "Producto", "Precio con IVA", "Costo", "Existencias", "Talla"],
    ["A1", "Tenis Nike Air", 250000, 150000, 4, "38-42"],
    ["A2", "Camiseta básica", "35.000", 12000, 0, "S-XL"],
]


def test_prices_are_read_as_colombians_write_them():
    assert parse_price("$ 25.000") == 25000
    assert parse_price("25,000.00") == 25000
    assert parse_price("25.000,50") == 25000
    assert parse_price(25000.0) == 25000
    assert parse_price("12") is None
    assert parse_price("") is None


def test_excel_columns_are_found_even_with_extra_columns():
    items, source = parse_upload("inventario.xlsx", _xlsx(INVENTORY_XLSX))
    assert source == "excel"
    assert items == [
        {"name": "Tenis Nike Air", "price_cop": 250000, "stock": 4, "details": "Talla: 38-42"},
        {"name": "Camiseta básica", "price_cop": 35000, "stock": 0, "details": "Talla: S-XL"},
    ]


def test_a_pdf_too_big_for_the_ai_is_rejected_before_calling_gemini():
    import pytest

    from app.application.products.product_import_service import ProductImportError

    with patch("app.application.products.product_import_service._ai_items") as mock_ai:
        with pytest.raises(ProductImportError, match="14 MB"):
            parse_upload("catalogo.pdf", b"%PDF" + b"x" * (14 * 1024 * 1024 + 1))
    mock_ai.assert_not_called()


def test_csv_word_and_text_lists():
    items, _ = parse_upload("lista.csv", "nombre;valor;descripcion\nPizza hawaiana;32.000;Grande\n".encode())
    assert items == [{"name": "Pizza hawaiana", "price_cop": 32000, "stock": None, "details": "descripcion: Grande"}]

    items, _ = parse_upload("sin_encabezado.csv", "Arepa de queso,4500\nPandebono,2500\n".encode("latin-1"))
    assert [(i["name"], i["price_cop"]) for i in items] == [("Arepa de queso", 4500), ("Pandebono", 2500)]

    items, source = parse_upload(
        "carta.docx", _docx(["Bandeja paisa ........ $28.000", "Horario: 8am a 8pm"], [["Plato", "Precio"], ["Ajiaco", "$22.000"]])
    )
    assert source == "word"
    assert {(i["name"], i["price_cop"]) for i in items} == {("Ajiaco", 22000), ("Bandeja paisa", 28000)}

    items, _ = parse_upload("precios.txt", "Corte de cabello — $25.000 — incluye lavado\nTinte 80.000\n".encode())
    assert [(i["name"], i["price_cop"]) for i in items] == [("Corte de cabello", 25000), ("Tinte", 80000)]


def test_unsupported_or_broken_files_say_why():
    import pytest

    from app.application.products.product_import_service import ProductImportError

    with pytest.raises(ProductImportError):
        parse_upload("base.sql", b"INSERT INTO x")
    with pytest.raises(ProductImportError):
        parse_upload("roto.xlsx", b"no es un zip")


def _product(name, price=None, details=None):
    return Product(id=uuid.uuid4(), name=name, price_cop=price, stock=None, details=details)


def test_big_catalog_shows_the_ai_what_the_customer_asked_for():
    catalog = [_product(f"Referencia genérica {i}", 10000 + i) for i in range(500)]
    catalog[450] = _product("Tenis Puma rojos", 199000, "tallas 38 a 43")
    assert rank_products(catalog, "hola, ¿tienes los tenis puma?")[0].name == "Tenis Puma rojos"

    block = products_prompt_block(catalog, selling=True, query="hola, ¿tienes los tenis puma?")
    assert "Tenis Puma rojos" in block
    assert "El catálogo tiene 500 productos" in block
    assert "NO digas que no lo hay" in block

    small = products_prompt_block(catalog[:5], selling=True, query="tenis")
    assert "El catálogo tiene" not in small


@requires_db
def test_upload_preview_then_import_updates_existing(client: TestClient):
    auth = _register(client)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    r = client.post("/api/v1/products", json={"name": "tenis nike air", "price_cop": 200000}, headers=headers)
    assert r.status_code == 201, r.text

    r = client.post(
        "/api/v1/products/import-file",
        files={"file": ("inventario.xlsx", _xlsx(INVENTORY_XLSX), "application/octet-stream")},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["total"] == 2 and preview["new"] == 1 and preview["existing"] == 1
    assert [i["exists"] for i in preview["items"]] == [True, False]
    assert len(client.get("/api/v1/products", headers=headers).json()) == 1

    r = client.post(
        "/api/v1/products/import-file",
        files={"file": ("vacio.csv", b"hola;mundo\n", "text/csv")},
        headers=headers,
    )
    assert r.status_code == 422

    r = client.post(
        "/api/v1/products/import-items",
        json={"items": preview["items"], "update_existing": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["created"], body["updated"]) == (1, 1)
    by_name = {p["name"].lower(): p for p in body["products"]}
    assert by_name["tenis nike air"]["price_cop"] == 250000
    assert by_name["camiseta básica"]["stock"] == 0

    r = client.post(
        "/api/v1/products/import-items",
        json={"items": preview["items"], "update_existing": False},
        headers=headers,
    )
    assert (r.json()["created"], r.json()["updated"]) == (0, 0)
    assert len(r.json()["products"]) == 2


def _add_open_questions(tenant_id: uuid.UUID, topics: list[str], *, age: timedelta) -> None:
    from app.domain.entities import AiQuestion
    from app.infrastructure.persistence.database import SessionLocal

    when = datetime.now(timezone.utc) - age
    with SessionLocal() as db:
        for topic in topics:
            db.add(
                AiQuestion(
                    tenant_id=tenant_id,
                    topic=topic,
                    topic_key=topic.lower(),
                    question=f"¿{topic}?",
                    status="open",
                    times_asked=2,
                    last_asked_at=when,
                    created_at=when,
                )
            )
        db.commit()


@requires_db
def test_weekly_summary_lists_what_the_ai_did_not_know(client: TestClient):
    from app.application.results.weekly_report import send_weekly_reports
    from app.infrastructure.persistence.database import SessionLocal

    from tests.test_platform_console import _register as register_named

    owner = register_named(client, "semanal")
    tenant_id = uuid.UUID(owner["tenant_id"])
    _add_open_questions(tenant_id, ["Precio del envío a Cali"], age=timedelta(days=2))
    client.post("/api/v1/products", json={"name": "Tenis sin precio"}, headers=owner["headers"])
    email = owner["email"]

    from app.application.appointments.appointment_service import BOGOTA

    today = datetime.now(BOGOTA).date()
    monday = today + timedelta(days=7 - today.weekday())
    monday_9am = datetime(monday.year, monday.month, monday.day, 9, 0, tzinfo=BOGOTA)
    sunday = monday_9am - timedelta(days=1)
    with patch("app.infrastructure.email.email_service.send_billing_email") as send, patch(
        "app.application.results.weekly_report._is_live", return_value=True
    ):
        with SessionLocal() as db:
            assert send_weekly_reports(db, now=sunday) == 0
            send_weekly_reports(db, now=monday_9am)
            send_weekly_reports(db, now=monday_9am + timedelta(minutes=15))
    mine = [c.kwargs for c in send.call_args_list if c.kwargs["to_email"] == email]
    assert len(mine) == 1
    text = " ".join(mine[0]["paragraphs"])
    assert "no supo responder 1 pregunta" in text
    assert "Precio del envío a Cali" in text
    assert "1 de tus 1 productos no tienen precio" in text


@requires_db
def test_console_flags_ai_that_keeps_not_knowing(client: TestClient, admin_and_client):  # noqa: F811
    admin, customer = admin_and_client
    tenant_id = uuid.UUID(customer["tenant_id"])
    _add_open_questions(tenant_id, ["Envíos", "Garantía"], age=timedelta(days=2))
    _add_open_questions(tenant_id, ["Recién preguntado"], age=timedelta(hours=1))

    def reasons():
        detail = client.get(f"/api/v1/platform/tenants/{customer['tenant_id']}", headers=admin["headers"]).json()
        return {r["key"]: r for r in detail["attention"]}

    assert "ai_unanswered" not in reasons()
    _add_open_questions(tenant_id, ["Domicilios"], age=timedelta(days=3))
    flagged = reasons()["ai_unanswered"]
    assert flagged["detail"] == "3" and flagged["priority"] == 2

    [row] = client.get(
        "/api/v1/platform/tenants", headers=admin["headers"], params={"q": customer["email"]}
    ).json()
    assert "ai_unanswered" in [r["key"] for r in row["attention"]]

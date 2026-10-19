"""Subir el catálogo completo de una vez: Excel, CSV, Word, PDF o fotos → lista de productos con precio."""
from __future__ import annotations

import base64
import csv
import io
import re
import uuid
import zipfile
from typing import Any, Optional
from xml.etree import ElementTree

from sqlalchemy.orm import Session

from app.application.products.product_service import (
    MAX_PRODUCTS,
    _name_key,
    create_product,
    list_products,
    parse_catalog_lines,
    update_product,
)

MAX_IMPORT_BYTES = 15 * 1024 * 1024
MAX_IMPORT_ROWS = MAX_PRODUCTS

SPREADSHEET_EXT = {"xlsx", "xlsm"}
TEXT_TABLE_EXT = {"csv", "tsv"}
WORD_EXT = {"docx"}
PLAIN_EXT = {"txt"}
AI_EXT = {"pdf", "jpg", "jpeg", "png", "webp", "heic"}
SUPPORTED_EXT = SPREADSHEET_EXT | TEXT_TABLE_EXT | WORD_EXT | PLAIN_EXT | AI_EXT

_AI_MIME = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "heic": "image/heic",
}

# Cómo suelen llamar los negocios a cada columna (sin tildes, en minúscula).
_NAME_HEADERS = ("producto", "nombre", "articulo", "item", "descripcion corta", "plato", "servicio", "referencia", "modelo")
_PRICE_HEADERS = ("precio venta", "precio de venta", "precio", "valor", "pvp", "venta", "tarifa", "price")
_STOCK_HEADERS = ("existencias", "inventario", "stock", "cantidad", "unidades", "disponible", "saldo")
_DETAIL_HEADERS = (
    "descripcion", "detalle", "detalles", "categoria", "talla", "tallas", "color", "colores",
    "presentacion", "marca", "medida", "medidas", "tamano", "sabor", "observaciones", "linea",
)
# Columnas que parecen precio pero no son lo que paga el cliente.
_NOT_PRICE = ("costo", "compra", "iva", "descuento", "utilidad", "margen", "codigo", "sku", "barras")

_SS_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ProductImportError(ValueError):
    pass


def file_extension(filename: str) -> str:
    return (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def parse_price(value: Any) -> Optional[int]:
    """«$ 25.000», «25,000.00», 25000.0 o «25.000,50» → 25000. Montos menores a 50 no son precios en pesos."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        amount = int(round(float(value)))
        return amount if 50 <= amount <= 1_000_000_000 else None
    text = str(value).strip().lower().replace("cop", "").replace("$", "").replace(" ", "")
    if not text or not re.search(r"\d", text):
        return None
    text = re.sub(r"[^\d.,]", "", text)
    if re.search(r"[.,]\d{1,2}$", text) and not re.search(r"[.,]\d{3}$", text):
        text = text[: max(text.rfind(","), text.rfind("."))]
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    amount = int(digits)
    return amount if 50 <= amount <= 1_000_000_000 else None


def _parse_stock(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        amount = int(float(str(value).replace(",", ".").strip()))
    except ValueError:
        return None
    return amount if 0 <= amount <= 1_000_000 else None


def _plain(text: Any) -> str:
    return _name_key(str(text or ""))


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return " ".join(str(value).split())


def _match_header(cell: str, options: tuple[str, ...]) -> bool:
    plain = _plain(cell)
    return bool(plain) and any(plain == opt or plain.startswith(opt + " ") or f" {opt}" in f" {plain}" for opt in options)


def _find_header(rows: list[list[Any]]) -> Optional[tuple[int, dict[str, Any]]]:
    for index, row in enumerate(rows[:15]):
        cols: dict[str, Any] = {"details": []}
        for col, cell in enumerate(row):
            text = _cell_text(cell)
            if not text:
                continue
            plain = _plain(text)
            if any(word in plain for word in _NOT_PRICE) and not plain.startswith(("precio", "valor", "pvp")):
                continue
            if "name" not in cols and _match_header(text, _NAME_HEADERS):
                cols["name"] = col
            elif "price" not in cols and _match_header(text, _PRICE_HEADERS):
                cols["price"] = col
            elif "stock" not in cols and _match_header(text, _STOCK_HEADERS):
                cols["stock"] = col
            elif _match_header(text, _DETAIL_HEADERS):
                cols["details"].append((col, text))
        if "name" not in cols and "price" in cols and cols["details"]:
            # «Descripción | Precio»: la descripción es el nombre.
            cols["name"] = cols["details"].pop(0)[0]
        if "name" in cols and "price" in cols:
            return index, cols
    return None


def _guess_columns(rows: list[list[Any]]) -> Optional[dict[str, Any]]:
    """Sin encabezados: el nombre es la columna con más texto y el precio la de montos en pesos."""
    sample = [r for r in rows[:50] if any(_cell_text(c) for c in r)]
    if not sample:
        return None
    width = max(len(r) for r in sample)
    text_score = [0] * width
    price_score = [0] * width
    for row in sample:
        for col in range(width):
            cell = row[col] if col < len(row) else None
            text = _cell_text(cell)
            if not text:
                continue
            if parse_price(cell) is not None and not re.search(r"[a-zA-Z]{3,}", text):
                price_score[col] += 1
            elif re.search(r"[a-zA-ZáéíóúñÁÉÍÓÚÑ]{2,}", text):
                text_score[col] += 1
    name_col = max(range(width), key=lambda c: text_score[c])
    price_col = max(range(width), key=lambda c: price_score[c])
    if text_score[name_col] < 2 or price_score[price_col] < 2 or name_col == price_col:
        return None
    return {"name": name_col, "price": price_col, "details": []}


def rows_to_items(rows: list[list[Any]]) -> list[dict]:
    rows = [list(r) for r in rows if r is not None]
    header = _find_header(rows)
    if header is not None:
        start, cols = header[0] + 1, header[1]
    else:
        cols = _guess_columns(rows)
        start = 0
        if cols is None:
            return []
    items: list[dict] = []
    for row in rows[start:]:
        def cell(col: Optional[int]) -> Any:
            return row[col] if col is not None and col < len(row) else None

        name = _cell_text(cell(cols.get("name")))[:120]
        if not name or parse_price(name) is not None and not re.search(r"[a-zA-Z]", name):
            continue
        details = []
        for col, label in cols.get("details", []):
            value = _cell_text(cell(col))
            if value:
                details.append(f"{label}: {value}" if header is not None else value)
        items.append(
            {
                "name": name,
                "price_cop": parse_price(cell(cols.get("price"))),
                "stock": _parse_stock(cell(cols.get("stock"))) if "stock" in cols else None,
                "details": " · ".join(details)[:500] or None,
            }
        )
        if len(items) >= MAX_IMPORT_ROWS:
            break
    return items


def _xlsx_rows(data: bytes) -> list[list[list[Any]]]:
    """Todas las hojas del libro, sin librerías externas (el .xlsx es un zip con XML)."""
    try:
        book = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ProductImportError("El archivo de Excel está dañado o no es .xlsx") from exc
    shared: list[str] = []
    if "xl/sharedStrings.xml" in book.namelist():
        root = ElementTree.fromstring(book.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", _SS_NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{_SS_NS['m']}}}t")))
    sheets = sorted(n for n in book.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    out: list[list[list[Any]]] = []
    for name in sheets:
        root = ElementTree.fromstring(book.read(name))
        rows: list[list[Any]] = []
        for row in root.iter(f"{{{_SS_NS['m']}}}row"):
            values: dict[int, Any] = {}
            for c in row.findall("m:c", _SS_NS):
                letters = re.match(r"[A-Z]+", c.get("r") or "")
                if letters:
                    col = 0
                    for ch in letters.group(0):
                        col = col * 26 + (ord(ch) - 64)
                    col -= 1
                else:
                    col = len(values)
                kind = c.get("t")
                v = c.find("m:v", _SS_NS)
                if kind == "s" and v is not None and v.text is not None:
                    idx = int(v.text)
                    values[col] = shared[idx] if idx < len(shared) else ""
                elif kind == "inlineStr":
                    values[col] = "".join(t.text or "" for t in c.iter(f"{{{_SS_NS['m']}}}t"))
                elif v is not None and v.text is not None:
                    try:
                        values[col] = float(v.text)
                    except ValueError:
                        values[col] = v.text
            if values:
                width = max(values) + 1
                rows.append([values.get(i) for i in range(width)])
        out.append(rows)
    return out


def _csv_rows(data: bytes) -> list[list[Any]]:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _docx_content(data: bytes) -> tuple[list[list[list[Any]]], str]:
    """Tablas y párrafos de un Word: las tablas se leen como Excel, el texto como lista de precios."""
    try:
        book = zipfile.ZipFile(io.BytesIO(data))
        root = ElementTree.fromstring(book.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ProductImportError("El archivo de Word está dañado o no es .docx") from exc
    tables: list[list[list[Any]]] = []
    for tbl in root.iter(f"{_W_NS}tbl"):
        rows = []
        for tr in tbl.iter(f"{_W_NS}tr"):
            rows.append(["".join(t.text or "" for t in tc.iter(f"{_W_NS}t")) for tc in tr.iter(f"{_W_NS}tc")])
        tables.append(rows)
    in_tables = {id(p) for tbl in root.iter(f"{_W_NS}tbl") for p in tbl.iter(f"{_W_NS}p")}
    lines = [
        "".join(t.text or "" for t in p.iter(f"{_W_NS}t"))
        for p in root.iter(f"{_W_NS}p")
        if id(p) not in in_tables
    ]
    return tables, "\n".join(line for line in lines if line.strip())


_TRAILING_PRICE_RE = re.compile(r"^(?P<name>.*?[A-Za-zÁÉÍÓÚÑáéíóúñ].*?)[\s.:…·-]*\$?\s*(?P<price>\d{1,3}(?:[.,]\d{3})+|\d{4,})\s*$")


def text_to_items(text: str) -> list[dict]:
    """«Nombre — $25.000 — detalle» o simplemente «Nombre ...... 25.000»."""
    items = parse_catalog_lines(text)
    seen = {_name_key(i["name"]) for i in items}
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-•*· ").strip()
        match = _TRAILING_PRICE_RE.match(line)
        if not match:
            continue
        name = " ".join(match.group("name").split()).rstrip(" :.-")[:120]
        key = _name_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        items.append({"name": name, "price_cop": parse_price(match.group("price")), "stock": None, "details": None})
    for item in items:
        item.setdefault("stock", None)
    return items[:MAX_IMPORT_ROWS]


def _ai_items(data: bytes, ext: str) -> list[dict]:
    from app.infrastructure.ai.gemini_client import GeminiError, extract_product_list

    try:
        text = extract_product_list(base64.b64encode(data).decode(), _AI_MIME[ext])
    except GeminiError as exc:
        raise ProductImportError("No pudimos leer el archivo ahora mismo. Intenta de nuevo en un rato.") from exc
    return text_to_items(text)


def parse_upload(filename: str, data: bytes) -> tuple[list[dict], str]:
    """(productos encontrados, cómo se leyó)."""
    ext = file_extension(filename)
    if ext not in SUPPORTED_EXT:
        if ext in {"xls", "doc"}:
            raise ProductImportError("Guarda el archivo como .xlsx (Excel) o .docx (Word) y vuelve a subirlo")
        raise ProductImportError("Sube un Excel (.xlsx), CSV, Word (.docx), PDF o foto de tu lista de precios")
    if not data:
        raise ProductImportError("El archivo está vacío")
    if len(data) > MAX_IMPORT_BYTES:
        raise ProductImportError("El archivo pesa más de 15 MB")
    if ext in SPREADSHEET_EXT:
        items: list[dict] = []
        for sheet in _xlsx_rows(data):
            items.extend(rows_to_items(sheet))
        return _dedupe(items), "excel"
    if ext in TEXT_TABLE_EXT:
        return _dedupe(rows_to_items(_csv_rows(data))), "csv"
    if ext in WORD_EXT:
        tables, text = _docx_content(data)
        items = [item for table in tables for item in rows_to_items(table)]
        items.extend(text_to_items(text))
        return _dedupe(items), "word"
    if ext in PLAIN_EXT:
        return _dedupe(text_to_items(data.decode("utf-8", errors="ignore"))), "texto"
    return _dedupe(_ai_items(data, ext)), "ia"


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        key = _name_key(item.get("name") or "")
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out[:MAX_IMPORT_ROWS]


def preview(db: Session, tenant_id: uuid.UUID, items: list[dict]) -> dict:
    existing = {_name_key(p.name) for p in list_products(db, tenant_id)}
    for item in items:
        item["exists"] = _name_key(item["name"]) in existing
    return {
        "items": items,
        "total": len(items),
        "with_price": sum(1 for i in items if i.get("price_cop")),
        "new": sum(1 for i in items if not i["exists"]),
        "existing": sum(1 for i in items if i["exists"]),
        "room": max(0, MAX_PRODUCTS - len(existing)),
    }


def import_items(db: Session, tenant_id: uuid.UUID, items: list[dict], *, update_existing: bool = True) -> dict:
    """Crea los nuevos y, si se pide, actualiza precio/cantidad/detalle de los que ya estaban (mismo nombre)."""
    by_key = {_name_key(p.name): p for p in list_products(db, tenant_id)}
    created = updated = skipped = 0
    full = False
    for item in items[:MAX_IMPORT_ROWS]:
        name = " ".join(str(item.get("name") or "").split())[:120]
        key = _name_key(name)
        if not key:
            skipped += 1
            continue
        price = parse_price(item.get("price_cop"))
        stock = _parse_stock(item.get("stock"))
        details = (str(item.get("details") or "").strip() or None)
        current = by_key.get(key)
        if current is not None:
            if not update_existing:
                skipped += 1
                continue
            changes: dict[str, Any] = {}
            if price is not None and price != current.price_cop:
                changes["price_cop"] = price
            if stock is not None and stock != current.stock:
                changes["stock"] = stock
            if details and not current.details:
                changes["details"] = details
            if changes:
                update_product(db, current, changes)
                updated += 1
            else:
                skipped += 1
            continue
        if full:
            skipped += 1
            continue
        try:
            by_key[key] = create_product(
                db, tenant_id=tenant_id, name=name, price_cop=price, stock=stock, details=details
            )
            created += 1
        except ValueError:
            full = True
            skipped += 1
    return {"created": created, "updated": updated, "skipped": skipped, "limit_reached": full}

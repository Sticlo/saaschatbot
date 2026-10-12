"""Productos del negocio: lo que la IA ofrece, a qué precio y cuántos quedan."""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.application.outbound.tenant_asset_service import save_product_photo
from app.domain.entities import Product

MAX_PRODUCTS = 300
MAX_PHOTOS = 4
_PROMPT_MAX_CHARS = 7000
_MAX_QTY = 999


@dataclass(frozen=True)
class StockChange:
    name: str
    sold: int
    left: int


def product_ref(product: Product) -> str:
    return product.id.hex[:8]


def list_products(db: Session, tenant_id: uuid.UUID) -> list[Product]:
    return (
        db.query(Product)
        .filter(Product.tenant_id == tenant_id)
        .order_by(Product.position.asc(), Product.created_at.asc())
        .all()
    )


def product_to_dict(product: Product) -> dict:
    return {
        "id": str(product.id),
        "name": product.name,
        "price_cop": product.price_cop,
        "stock": product.stock,
        "details": product.details or "",
        "photos": [path.rsplit("/", 1)[-1] for path in product.photos or []],
    }


def find_by_ref(products: list[Product], ref: Optional[str]) -> Optional[Product]:
    return next((p for p in products if ref and product_ref(p) == ref), None)


def add_photo(db: Session, product: Product, path: str) -> Product:
    photos = list(product.photos or [])
    if len(photos) >= MAX_PHOTOS:
        raise ValueError(f"Cada producto puede tener hasta {MAX_PHOTOS} fotos. Quita una para subir otra.")
    # Lista nueva: JSONB no detecta cambios hechos sobre la misma lista.
    product.photos = [*photos, path]
    db.flush()
    return product


def attach_uploaded_photo(db: Session, product: Product, upload: UploadFile) -> Product:
    if len(product.photos or []) >= MAX_PHOTOS:
        raise ValueError(f"Cada producto puede tener hasta {MAX_PHOTOS} fotos. Quita una para subir otra.")
    return add_photo(db, product, save_product_photo(product.tenant_id, upload))


def remove_photo(db: Session, product: Product, name: str) -> Optional[str]:
    """Quita la foto por nombre de archivo. Devuelve su ruta para borrar el archivo."""
    photos = list(product.photos or [])
    path = next((p for p in photos if p.rsplit("/", 1)[-1] == name), None)
    if path is None:
        return None
    product.photos = [p for p in photos if p != path]
    db.flush()
    return path


def photo_caption(product: Product) -> str:
    if product.price_cop is None:
        return product.name
    return f"{product.name} — {format_cop(product.price_cop)}"


def _clean_name(name: str) -> str:
    return " ".join((name or "").split())[:120]


def _clean_details(details: Optional[str]) -> Optional[str]:
    text = " ".join((details or "").split())[:500]
    return text or None


def _check_numbers(price_cop: Optional[int], stock: Optional[int]) -> None:
    if price_cop is not None and not 0 <= price_cop <= 1_000_000_000:
        raise ValueError("Revisa el precio")
    if stock is not None and not 0 <= stock <= 1_000_000:
        raise ValueError("Revisa cuántos quedan")


def create_product(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    name: str,
    price_cop: Optional[int] = None,
    stock: Optional[int] = None,
    details: Optional[str] = None,
) -> Product:
    clean = _clean_name(name)
    if not clean:
        raise ValueError("Escribe el nombre del producto")
    _check_numbers(price_cop, stock)
    count = db.query(func.count(Product.id)).filter(Product.tenant_id == tenant_id).scalar() or 0
    if count >= MAX_PRODUCTS:
        raise ValueError(f"Puedes tener hasta {MAX_PRODUCTS} productos")
    product = Product(
        tenant_id=tenant_id,
        name=clean,
        price_cop=price_cop,
        stock=stock,
        details=_clean_details(details),
        position=count,
    )
    db.add(product)
    db.flush()
    return product


def get_product(db: Session, tenant_id: uuid.UUID, product_id: uuid.UUID) -> Product:
    product = db.query(Product).filter(Product.id == product_id, Product.tenant_id == tenant_id).first()
    if product is None:
        raise LookupError("Producto no encontrado")
    return product


def update_product(db: Session, product: Product, changes: dict) -> Product:
    if "name" in changes:
        clean = _clean_name(changes["name"] or "")
        if not clean:
            raise ValueError("Escribe el nombre del producto")
        product.name = clean
    _check_numbers(changes.get("price_cop"), changes.get("stock"))
    if "price_cop" in changes:
        product.price_cop = changes["price_cop"]
    if "stock" in changes:
        product.stock = changes["stock"]
    if "details" in changes:
        product.details = _clean_details(changes["details"])
    db.flush()
    return product


def _name_key(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in plain if not unicodedata.combining(c)).lower()
    return " ".join(re.findall(r"\w+", plain))


_LINE_SPLIT_RE = re.compile(r"\s+[—–-]\s+|\s*\|\s*")
_PRICE_RE = re.compile(r"\$\s*(\d{1,3}(?:[.,]\d{3})+|\d{3,})|(\d{1,3}(?:[.,]\d{3})+)")
_BULLET_RE = re.compile(r"^\s*(?:[-•*·]+|\d+[.)])\s*")


def _price_of(text: str) -> Optional[int]:
    match = _PRICE_RE.search(text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1) or match.group(2))
    return int(digits) if digits else None


def parse_catalog_lines(catalog_text: str) -> list[dict]:
    """Gemini deja el catálogo como «Nombre — precio — detalles»: una línea por producto.
    Solo cuenta como producto lo que tiene precio; títulos y horarios quedan por fuera."""
    found: list[dict] = []
    for raw_line in (catalog_text or "").splitlines():
        line = _BULLET_RE.sub("", raw_line).strip()
        parts = [p.strip() for p in _LINE_SPLIT_RE.split(line) if p.strip()]
        if len(parts) < 2:
            continue
        name, rest = parts[0].rstrip(":"), parts[1:]
        price_index = next((i for i, part in enumerate(rest) if _price_of(part) is not None), None)
        if price_index is None or not name or _price_of(name) is not None:
            continue
        details = [part for i, part in enumerate(rest) if i != price_index]
        found.append(
            {"name": name, "price_cop": _price_of(rest[price_index]), "details": " · ".join(details) or None}
        )
    return found


def import_from_catalog(db: Session, tenant_id: uuid.UUID, catalog_texts: list[str]) -> int:
    """Crea los productos que se leen en los catálogos subidos. No toca los que ya existen."""
    existing = {_name_key(p.name) for p in list_products(db, tenant_id)}
    created = 0
    for text in catalog_texts:
        for item in parse_catalog_lines(text):
            key = _name_key(item["name"])
            if not key or key in existing:
                continue
            try:
                create_product(db, tenant_id=tenant_id, **item)
            except ValueError:
                return created
            existing.add(key)
            created += 1
    return created


def format_cop(amount: int) -> str:
    return "$" + f"{amount:,}".replace(",", ".")


def _availability(product: Product) -> str:
    if product.stock is None:
        return "disponible"
    if product.stock <= 0:
        return "AGOTADO"
    return f"quedan {product.stock}"


def products_prompt_block(products: list[Product], *, selling: bool) -> str:
    if not products:
        return ""
    lines: list[str] = []
    used = 0
    for product in products:
        price = format_cop(product.price_cop) if product.price_cop is not None else "precio por confirmar"
        line = f"- ref={product_ref(product)} | {product.name} | {price} | {_availability(product)}"
        if product.details:
            line += f" | {product.details}"
        if product.photos:
            line += f" | 📷 {len(product.photos)} foto{'' if len(product.photos) == 1 else 's'}"
        used += len(line)
        if used > _PROMPT_MAX_CHARS:
            break
        lines.append(line)
    rules = [
        "- Precios: cotiza SIEMPRE con el precio de esta lista. Si el catálogo, la carta o los rangos de precios "
        "dicen otra cosa, manda esta lista (el catálogo puede estar desactualizado).",
        "- Esta lista está al día: no necesitas confirmar con nadie si hay.",
        "- Ofrece y vende solo lo disponible. Si algo está AGOTADO, dilo y ofrece lo más parecido de la lista.",
        "- Si piden más unidades de las que quedan, ofrece las que hay.",
        "- El total es precio × cantidad (más el envío, si el negocio lo cobra y sabes cuánto es).",
        "- Si piden algo que no está en la lista, no lo inventes: di que lo confirmas por aquí.",
    ]
    if any(p.photos for p in products):
        rules.append(
            "- Fotos: si piden fotos o ver un producto marcado con 📷, pon su ref en photo_ref y escribe algo breve "
            "(«¡Claro! Mira 👇»): las fotos salen aparte con el nombre y el precio. Si ya las enviaste en este chat, "
            "no las repitas salvo que las pidan de nuevo."
        )
    rules.append(
        "- Si piden fotos de algo sin 📷 y ningún atajo las tiene, no prometas enviarlas: descríbelo con lo que "
        "sabes y pon en missing «fotos de <producto>»."
    )
    if selling:
        rules.append(
            '- Cuando llenes order, agrega "lines" con cada producto de la lista que lleva: '
            '[{"ref":"<ref>","qty":<cantidad>}].'
        )
    return (
        "\n\nProductos del negocio (ref | nombre | precio | disponibilidad | detalles):\n"
        + "\n".join(lines)
        + "\n"
        + "\n".join(rules)
    )


def reserve_order_stock(db: Session, tenant_id: uuid.UUID, lines: tuple[tuple[str, int], ...]) -> list[StockChange]:
    """Descuenta lo que se vendió. Solo productos con conteo; nunca baja de 0."""
    wanted: dict[str, int] = {}
    for ref, qty in lines:
        wanted[ref] = min(wanted.get(ref, 0) + qty, _MAX_QTY)
    if not wanted:
        return []
    rows = (
        db.query(Product)
        .filter(Product.tenant_id == tenant_id, Product.stock.isnot(None))
        .with_for_update()
        .all()
    )
    changes: list[StockChange] = []
    for product in rows:
        qty = wanted.get(product_ref(product))
        if not qty:
            continue
        sold = min(qty, product.stock)
        product.stock -= sold
        changes.append(StockChange(name=product.name, sold=sold, left=product.stock))
    db.flush()
    return changes

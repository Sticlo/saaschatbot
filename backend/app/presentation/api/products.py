from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from sqlalchemy.orm import Session

from app.application.ai.ai_readiness_service import autostart_ai_for_tenant_id
from app.application.billing.tenant_service import log_audit
from app.application.outbound.quick_shortcut_service import get_shortcuts
from app.application.outbound.tenant_asset_service import delete_asset
from app.application.products.product_service import (
    attach_uploaded_photo,
    create_product,
    get_product,
    import_from_catalog,
    list_products,
    product_to_dict,
    remove_photo,
    update_product,
)
from app.infrastructure.persistence.database import get_db
from app.application.products.product_import_service import (
    MAX_IMPORT_BYTES,
    ProductImportError,
    import_items,
    parse_upload,
    preview,
)
from app.presentation.schemas.products import (
    ProductCreate,
    ProductFilePreview,
    ProductImportResponse,
    ProductItemsImport,
    ProductItemsImportResponse,
    ProductResponse,
    ProductUpdate,
)
from app.shared.core.deps import RequireOwner, RequireViewer
from app.shared.core.rate_limit import enforce_rate_limit

router = APIRouter(prefix="/products", tags=["products"])


def _all(db: Session, tenant_id: uuid.UUID) -> list[ProductResponse]:
    return [ProductResponse(**product_to_dict(p)) for p in list_products(db, tenant_id)]


@router.get("", response_model=list[ProductResponse])
def get_products(current: RequireViewer, db: Session = Depends(get_db)):
    return _all(db, current.tenant_id)


@router.post("", response_model=ProductResponse, status_code=201)
def post_product(body: ProductCreate, current: RequireOwner, db: Session = Depends(get_db)):
    try:
        product = create_product(db, tenant_id=current.tenant_id, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    autostart_ai_for_tenant_id(db, current.tenant_id)
    return ProductResponse(**product_to_dict(product))


@router.patch("/{product_id}", response_model=ProductResponse)
def patch_product(
    product_id: uuid.UUID, body: ProductUpdate, current: RequireOwner, db: Session = Depends(get_db)
):
    try:
        product = get_product(db, current.tenant_id, product_id)
        # Solo lo que mandó el panel: «stock: null» es «no llevo la cuenta», no «no lo toques».
        update_product(db, product, body.model_dump(exclude_unset=True))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    autostart_ai_for_tenant_id(db, current.tenant_id)
    return ProductResponse(**product_to_dict(product))


@router.delete("/{product_id}", status_code=204)
def delete_product(
    product_id: uuid.UUID, request: Request, current: RequireOwner, db: Session = Depends(get_db)
):
    try:
        product = get_product(db, current.tenant_id, product_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    photos = list(product.photos or [])
    # La copia en el registro permite rehacer a mano un producto borrado por error.
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="products.deleted",
        details={key: value for key, value in product_to_dict(product).items() if key != "photos"},
        ip_address=request.client.host if request.client else None,
    )
    db.delete(product)
    db.commit()
    for path in photos:
        delete_asset(path, tenant_id=current.tenant_id)
    return Response(status_code=204)


def enforce_photo_rate_limit(tenant_id: uuid.UUID) -> None:
    enforce_rate_limit(
        f"products:photo:{tenant_id}",
        limit=60,
        window_seconds=3600,
        message="Subiste muchas fotos seguidas. Intenta de nuevo en un rato.",
    )


@router.post("/{product_id}/photos", response_model=ProductResponse)
def post_product_photo(
    product_id: uuid.UUID, current: RequireOwner, file: UploadFile = File(...), db: Session = Depends(get_db)
):
    enforce_photo_rate_limit(current.tenant_id)
    try:
        product = attach_uploaded_photo(db, get_product(db, current.tenant_id, product_id), file)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return ProductResponse(**product_to_dict(product))


@router.delete("/{product_id}/photos/{name}", response_model=ProductResponse)
def delete_product_photo(product_id: uuid.UUID, name: str, current: RequireOwner, db: Session = Depends(get_db)):
    try:
        product = get_product(db, current.tenant_id, product_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    path = remove_photo(db, product, name)
    if path is None:
        raise HTTPException(status_code=404, detail="Foto no encontrada")
    db.commit()
    delete_asset(path, tenant_id=current.tenant_id)
    return ProductResponse(**product_to_dict(product))


@router.post("/import-file", response_model=ProductFilePreview)
def import_file_preview(current: RequireOwner, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Lee el archivo y muestra qué productos se van a crear; no guarda nada todavía."""
    enforce_rate_limit(
        f"products:import:{current.tenant_id}",
        limit=20,
        window_seconds=3600,
        message="Subiste muchos archivos seguidos. Intenta de nuevo en un rato.",
    )
    data = file.file.read(MAX_IMPORT_BYTES + 1)
    try:
        items, source = parse_upload(file.filename or "", data)
    except ProductImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not items:
        raise HTTPException(
            status_code=422,
            detail="No encontramos productos con nombre y precio. Revisa que el archivo tenga una columna "
            "de producto y otra de precio.",
        )
    return ProductFilePreview(**preview(db, current.tenant_id, items), source=source)


@router.post("/import-items", response_model=ProductItemsImportResponse)
def import_items_route(body: ProductItemsImport, current: RequireOwner, db: Session = Depends(get_db)):
    result = import_items(
        db,
        current.tenant_id,
        [item.model_dump() for item in body.items],
        update_existing=body.update_existing,
    )
    db.commit()
    autostart_ai_for_tenant_id(db, current.tenant_id)
    return ProductItemsImportResponse(**result, products=_all(db, current.tenant_id))


@router.post("/import-catalog", response_model=ProductImportResponse)
def import_catalog(current: RequireOwner, db: Session = Depends(get_db)):
    """Arma la lista con lo que la IA ya leyó de los catálogos subidos (fotos o PDF)."""
    texts = [str(s.get("content") or "") for s in get_shortcuts(db, current.tenant_id) if s.get("content")]
    if not texts:
        raise HTTPException(status_code=422, detail="Primero sube tu catálogo o carta en Atajos rápidos")
    created = import_from_catalog(db, current.tenant_id, texts)
    db.commit()
    autostart_ai_for_tenant_id(db, current.tenant_id)
    return ProductImportResponse(created=created, products=_all(db, current.tenant_id))

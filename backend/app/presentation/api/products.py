from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.application.outbound.quick_shortcut_service import get_shortcuts
from app.application.products.product_service import (
    create_product,
    get_product,
    import_from_catalog,
    list_products,
    product_to_dict,
    update_product,
)
from app.infrastructure.persistence.database import get_db
from app.presentation.schemas.products import (
    ProductCreate,
    ProductImportResponse,
    ProductResponse,
    ProductUpdate,
)
from app.shared.core.deps import RequireOwner, RequireViewer

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
    return ProductResponse(**product_to_dict(product))


@router.delete("/{product_id}", status_code=204)
def delete_product(product_id: uuid.UUID, current: RequireOwner, db: Session = Depends(get_db)):
    try:
        db.delete(get_product(db, current.tenant_id, product_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return Response(status_code=204)


@router.post("/import-catalog", response_model=ProductImportResponse)
def import_catalog(current: RequireOwner, db: Session = Depends(get_db)):
    """Arma la lista con lo que la IA ya leyó de los catálogos subidos (fotos o PDF)."""
    texts = [str(s.get("content") or "") for s in get_shortcuts(db, current.tenant_id) if s.get("content")]
    if not texts:
        raise HTTPException(status_code=422, detail="Primero sube tu catálogo o carta en Atajos rápidos")
    created = import_from_catalog(db, current.tenant_id, texts)
    db.commit()
    return ProductImportResponse(created=created, products=_all(db, current.tenant_id))

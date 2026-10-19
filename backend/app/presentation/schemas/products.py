from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    price_cop: Optional[int] = Field(default=None, ge=0, le=1_000_000_000)
    stock: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    details: Optional[str] = Field(default=None, max_length=500)


class ProductUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    price_cop: Optional[int] = Field(default=None, ge=0, le=1_000_000_000)
    stock: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    details: Optional[str] = Field(default=None, max_length=500)


class ProductResponse(BaseModel):
    id: str
    name: str
    price_cop: Optional[int] = None
    stock: Optional[int] = None
    details: str = ""
    # Nombres de archivo; se ven en /api/v1/outbound/assets/<nombre>.
    photos: list[str] = Field(default_factory=list)


class ProductImportResponse(BaseModel):
    created: int
    products: list[ProductResponse]


class ImportedItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    price_cop: Optional[int] = Field(default=None, ge=0, le=1_000_000_000)
    stock: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    details: Optional[str] = Field(default=None, max_length=1000)
    exists: bool = False


class ProductFilePreview(BaseModel):
    items: list[ImportedItem]
    total: int
    with_price: int
    new: int
    existing: int
    room: int
    source: str


class ProductItemsImport(BaseModel):
    items: list[ImportedItem] = Field(max_length=2000)
    update_existing: bool = True


class ProductItemsImportResponse(BaseModel):
    created: int
    updated: int
    skipped: int
    limit_reached: bool
    products: list[ProductResponse]

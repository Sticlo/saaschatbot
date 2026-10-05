from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class QuickShortcutInput(BaseModel):
    id: Optional[str] = None
    label: str = Field(min_length=1, max_length=20)
    type: Literal["text", "image", "document"] = "text"
    text: Optional[str] = Field(default=None, max_length=2000)
    image_path: Optional[str] = Field(default=None, max_length=512)
    file_path: Optional[str] = Field(default=None, max_length=512)
    file_name: Optional[str] = Field(default=None, max_length=80)
    content: Optional[str] = Field(default=None, max_length=4000)


class QuickShortcutResponse(BaseModel):
    id: str
    label: str
    type: str
    text: Optional[str] = None
    image_path: Optional[str] = None
    image_url: Optional[str] = None
    file_path: Optional[str] = None
    file_name: Optional[str] = None
    file_url: Optional[str] = None
    content: Optional[str] = None


class QuickShortcutsUpdate(BaseModel):
    shortcuts: list[QuickShortcutInput] = Field(default_factory=list, max_length=12)


class CatalogFileUploadResponse(BaseModel):
    type: Literal["image", "document"]
    path: str
    url: str
    file_name: Optional[str] = None
    content: str = ""
    content_ok: bool = False

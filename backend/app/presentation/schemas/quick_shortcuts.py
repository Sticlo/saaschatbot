from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class QuickShortcutInput(BaseModel):
    id: Optional[str] = None
    label: str = Field(min_length=1, max_length=20)
    type: Literal["text", "image"] = "text"
    text: Optional[str] = Field(default=None, max_length=2000)
    image_path: Optional[str] = Field(default=None, max_length=512)


class QuickShortcutResponse(BaseModel):
    id: str
    label: str
    type: str
    text: Optional[str] = None
    image_path: Optional[str] = None
    image_url: Optional[str] = None


class QuickShortcutsUpdate(BaseModel):
    shortcuts: list[QuickShortcutInput] = Field(default_factory=list, max_length=12)

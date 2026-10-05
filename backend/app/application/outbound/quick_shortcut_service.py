from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.application.billing.tenant_profile_service import get_or_create_tenant_profile

_MAX_SHORTCUTS = 12
MAX_CONTENT_CHARS = 4000


def _normalize(raw: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in raw[:_MAX_SHORTCUTS]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        kind = str(item.get("type") or "text").strip().lower()
        if kind not in ("text", "image", "document"):
            kind = "text"
        sid = str(item.get("id") or uuid.uuid4())
        # Lo que la IA sabe del archivo (productos, precios) para responder sin reenviarlo.
        content = str(item.get("content") or "").strip()[:MAX_CONTENT_CHARS]
        if kind == "image":
            path = str(item.get("image_path") or "").strip() or None
            if not path:
                continue
            row = {"id": sid, "label": label[:20], "type": "image", "image_path": path}
            if content:
                row["content"] = content
            out.append(row)
        elif kind == "document":
            path = str(item.get("file_path") or "").strip() or None
            if not path:
                continue
            row = {
                "id": sid,
                "label": label[:20],
                "type": "document",
                "file_path": path,
                "file_name": str(item.get("file_name") or "catalogo.pdf").strip()[:80] or "catalogo.pdf",
            }
            if content:
                row["content"] = content
            out.append(row)
        else:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            out.append({"id": sid, "label": label[:20], "type": "text", "text": text[:2000]})
    return out


def get_shortcuts(db: Session, tenant_id: uuid.UUID) -> list[dict]:
    profile = get_or_create_tenant_profile(db, tenant_id)
    if not profile.quick_shortcuts:
        return []
    if isinstance(profile.quick_shortcuts, list):
        return profile.quick_shortcuts
    return []


def save_shortcuts(db: Session, tenant_id: uuid.UUID, shortcuts: list[dict]) -> list[dict]:
    profile = get_or_create_tenant_profile(db, tenant_id)
    cleaned = _normalize(shortcuts)
    profile.quick_shortcuts = cleaned
    db.flush()
    return cleaned


def find_shortcut(db: Session, tenant_id: uuid.UUID, shortcut_id: str) -> Optional[dict]:
    for item in get_shortcuts(db, tenant_id):
        if str(item.get("id")) == str(shortcut_id):
            return item
    return None
